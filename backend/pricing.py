"""
S3-compatible storage provider pricing — hybrid approach.

Pricing sources (in priority order):
1. AWS:  Live data from AWS Bulk Pricing API (free, no auth, authoritative)
2. Others: Static defaults from s3compare.io community dataset (CC BY 4.0)
3. Admin overrides stored in SQLite (highest priority when set)

WHY NOT ALL LIVE?
Only AWS exposes a pricing API. Cloudflare R2, Backblaze B2, Wasabi, Leaseweb,
and other S3-compatible providers do NOT have programmatic pricing endpoints.
Their prices are published on marketing pages and rarely change (<1x/year).

COMMUNITY NOTE:
If you know of a provider that now offers a pricing API, or if any prices here
are outdated, please open an issue or PR at https://github.com/objexstorage/objex.
We want this to be accurate and will happily integrate better data sources.

Static pricing last verified: 2026-04-11
Sources: https://www.s3compare.io/s3compare.io_data.csv (CC BY 4.0)
"""

import json
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("sairo.pricing")

# ── Static pricing (providers without APIs) ──────────────────────────────────
# Per GB per month, USD. Sourced from s3compare.io + provider docs.

STATIC_PRICING: dict[str, dict[str, float]] = {
    "aws": {
        "standard": 0.023,
        "intelligent_tiering": 0.023,
        "standard_ia": 0.0125,
        "one_zone_ia": 0.01,
        "glacier_instant": 0.004,
        "glacier_flexible": 0.0036,
        "glacier_deep_archive": 0.00099,
    },
    "r2": {
        "standard": 0.015,
    },
    "b2": {
        "standard": 0.006,
    },
    "wasabi": {
        "standard": 0.0069,
    },
    "minio": {
        "standard": 0.0,
    },
    "ceph": {
        "standard": 0.0,
    },
    "leaseweb": {
        "standard": 0.012,
    },
    "digitalocean": {
        "standard": 0.02,
    },
    "hetzner": {
        "standard": 0.0052,
    },
    "scaleway": {
        "standard": 0.016,
    },
    "ovh": {
        "standard": 0.011,
    },
    "idrive_e2": {
        "standard": 0.004,
    },
    "storj": {
        "standard": 0.004,
    },
}

# Region multipliers (AWS only — other providers have flat pricing)
REGION_MULTIPLIERS: dict[str, float] = {
    "us-east-1": 1.0, "us-east-2": 1.0, "us-west-1": 1.1, "us-west-2": 1.0,
    "eu-west-1": 1.08, "eu-west-2": 1.08, "eu-central-1": 1.08,
    "ap-southeast-1": 1.1, "ap-northeast-1": 1.14, "ap-south-1": 1.04,
    "sa-east-1": 1.22, "ca-central-1": 1.05, "af-south-1": 1.22,
    "me-south-1": 1.17,
}

# Minimum storage duration (days) before early-deletion charges
MIN_STORAGE_DURATION: dict[str, dict[str, int]] = {
    "aws": {
        "standard": 0, "standard_ia": 30, "one_zone_ia": 30,
        "glacier_instant": 90, "glacier_flexible": 90, "glacier_deep_archive": 180,
    },
    "wasabi": {"standard": 90},
}

# ── AWS Live Pricing Cache ───────────────────────────────────────────────────
_aws_pricing_cache: dict = {}
_aws_pricing_fetched_at: float = 0
_AWS_CACHE_TTL = 86400  # refresh once per day

# Map AWS SKU usageType patterns to our storage class names
_AWS_CLASS_MAP = {
    "TimedStorage-ByteHrs": "standard",
    "TimedStorage-INT-FA-ByteHrs": "intelligent_tiering",
    "TimedStorage-SIA-ByteHrs": "standard_ia",
    "TimedStorage-ZIA-ByteHrs": "one_zone_ia",
    "TimedStorage-GlacierInstantRetrieval": "glacier_instant",
    "TimedStorage-GlacierByteHrs": "glacier_flexible",
    "TimedStorage-GDA-ByteHrs": "glacier_deep_archive",
}


def _fetch_aws_pricing(region: str = "us-east-1") -> dict[str, float]:
    """Fetch live S3 storage pricing from AWS Bulk Pricing API (no auth required)."""
    url = f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/{region}/index.json"
    try:
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Collect all prices per class, then take the minimum (avoids tiered pricing artifacts)
        prices_all: dict[str, list[float]] = {}
        for sku_id, product in data.get("products", {}).items():
            attrs = product.get("attributes", {})
            usage_type = attrs.get("usagetype", "")

            for pattern, class_name in _AWS_CLASS_MAP.items():
                if pattern in usage_type:
                    on_demand = data.get("terms", {}).get("OnDemand", {}).get(sku_id, {})
                    for term in on_demand.values():
                        for dim in term.get("priceDimensions", {}).values():
                            price_str = dim.get("pricePerUnit", {}).get("USD", "0")
                            price = float(price_str)
                            if price > 0:
                                prices_all.setdefault(class_name, []).append(price)
                    break

        # Use the minimum price per class (general tier, not first-50TB premium)
        prices = {cls: min(vals) for cls, vals in prices_all.items() if vals}

        if prices:
            log.info("Fetched live AWS S3 pricing for %s: %d classes", region, len(prices))
            return prices
        else:
            log.warning("AWS pricing API returned no usable prices for %s, using static fallback", region)
            return {}
    except Exception as e:
        log.warning("Failed to fetch AWS pricing: %s — using static fallback", e)
        return {}


def _get_aws_pricing(region: str = "us-east-1") -> dict[str, float]:
    """Get AWS pricing with caching (fetches live at most once per day)."""
    global _aws_pricing_cache, _aws_pricing_fetched_at
    cache_key = region
    now = time.time()

    if cache_key in _aws_pricing_cache and (now - _aws_pricing_fetched_at) < _AWS_CACHE_TTL:
        return _aws_pricing_cache[cache_key]

    live = _fetch_aws_pricing(region)
    if live:
        _aws_pricing_cache[cache_key] = live
        _aws_pricing_fetched_at = now
        return live

    return STATIC_PRICING["aws"]


# ── Provider detection from endpoint URL ─────────────────────────────────────
_URL_TO_PROVIDER = [
    # AWS S3
    ("amazonaws.com", "aws"),
    ("s3.dualstack", "aws"),
    # Cloudflare R2
    ("r2.cloudflarestorage.com", "r2"),
    # Backblaze B2
    ("backblazeb2.com", "b2"),
    ("backblaze.com", "b2"),
    # Wasabi
    ("wasabisys.com", "wasabi"),
    ("wasabi.com", "wasabi"),
    # DigitalOcean Spaces
    ("digitaloceanspaces.com", "digitalocean"),
    # Leaseweb (StorageGRID)
    ("object-storage.io", "leaseweb"),
    ("objectstorage.leaseweb", "leaseweb"),
    # Hetzner
    ("storage.hetzner.com", "hetzner"),
    ("your-objectstorage.com", "hetzner"),
    # Scaleway
    ("scw.cloud", "scaleway"),
    ("scaleway.com", "scaleway"),
    # OVHcloud
    ("storage.cloud.ovh.net", "ovh"),
    ("ovh.net", "ovh"),
    ("ovh.io", "ovh"),
    # iDrive e2
    ("idrivee2", "idrive_e2"),
    ("idrive.com", "idrive_e2"),
    # Storj
    ("storj.io", "storj"),
    ("gateway.storjshare.io", "storj"),
    # Google Cloud Storage (S3-compatible interop)
    ("storage.googleapis.com", "gcs"),
    # Azure Blob (S3-compatible gateway)
    ("blob.core.windows.net", "azure"),
    # IBM Cloud Object Storage
    ("cloud-object-storage.appdomain.cloud", "ibm"),
    ("cloud.ibm.com", "ibm"),
    # Oracle Cloud
    ("oraclecloud.com", "oracle"),
    # Vultr
    ("vultrobjects.com", "vultr"),
    # Linode / Akamai
    ("linodeobjects.com", "linode"),
    # Tigris
    ("fly.storage.tigris.dev", "tigris"),
    ("tigris.dev", "tigris"),
    # NetApp StorageGRID (common deployments)
    ("storagegrid", "storagegrid"),
    # Self-hosted patterns
    ("minio", "minio"),
    ("localhost", "self_hosted"),
    ("127.0.0.1", "self_hosted"),
]

# Private/local IP ranges that suggest self-hosted MinIO/Ceph
import re as _re
_PRIVATE_IP_RE = _re.compile(
    r"://("
    r"10\.\d+\.\d+\.\d+|"            # 10.0.0.0/8
    r"192\.168\.\d+\.\d+|"           # 192.168.0.0/16
    r"172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+|"  # 172.16.0.0/12
    r"\[?::1\]?|"                    # IPv6 localhost
    r"\[?fc[0-9a-f]{2}:|"            # IPv6 unique local
    r"\[?fe80:"                      # IPv6 link-local
    r")"
)


def detect_provider(endpoint_url: str) -> str:
    """Infer storage provider from S3 endpoint URL."""
    if not endpoint_url:
        return "unknown"
    url_lower = endpoint_url.lower()
    for pattern, provider in _URL_TO_PROVIDER:
        if pattern in url_lower:
            return provider
    # Private/local IPs almost always mean self-hosted MinIO or Ceph
    if _PRIVATE_IP_RE.search(url_lower):
        return "self_hosted"
    return "unknown"


# ── Public API ───────────────────────────────────────────────────────────────

def get_storage_pricing(provider: str, region: str = "us-east-1") -> dict[str, float]:
    """Get storage class pricing for a provider. Returns {class_name: price_per_gb_month}."""
    provider = provider.lower()

    if provider == "aws":
        return _get_aws_pricing(region)

    return STATIC_PRICING.get(provider, {"standard": 0.0})


def get_storage_price(provider: str, storage_class: str = "standard", region: str = "us-east-1") -> float:
    """Get price per GB per month for a specific provider/class/region."""
    prices = get_storage_pricing(provider, region)
    return prices.get(storage_class.lower(), prices.get("standard", 0.0))


def estimate_monthly_cost(total_bytes: int, provider: str, region: str = "us-east-1") -> dict:
    """Estimate monthly cost across all storage classes for a provider."""
    total_gb = total_bytes / (1024 ** 3)
    prices = get_storage_pricing(provider, region)

    result = {}
    for storage_class, price_per_gb in prices.items():
        result[storage_class] = {
            "price_per_gb_month": round(price_per_gb, 6),
            "monthly_cost": round(total_gb * price_per_gb, 2),
            "annual_cost": round(total_gb * price_per_gb * 12, 2),
            "total_gb": round(total_gb, 2),
        }
    return result


def calculate_savings(
    total_bytes: int, cold_bytes: int, provider: str,
    current_class: str = "standard", target_class: str = "glacier_instant",
    region: str = "us-east-1",
) -> dict:
    """Calculate savings from moving cold data to a cheaper storage class."""
    hot_bytes = total_bytes - cold_bytes
    current_price = get_storage_price(provider, current_class, region)
    target_price = get_storage_price(provider, target_class, region)

    current_cost = (total_bytes / (1024 ** 3)) * current_price
    new_cost = (hot_bytes / (1024 ** 3)) * current_price + (cold_bytes / (1024 ** 3)) * target_price
    monthly_savings = current_cost - new_cost

    return {
        "current_monthly_cost": round(current_cost, 2),
        "projected_monthly_cost": round(new_cost, 2),
        "monthly_savings": round(monthly_savings, 2),
        "annual_savings": round(monthly_savings * 12, 2),
        "savings_pct": round((monthly_savings / current_cost * 100) if current_cost > 0 else 0, 1),
    }


def get_all_providers() -> dict:
    """Return all known providers with their pricing and metadata."""
    providers = {}
    for name, prices in STATIC_PRICING.items():
        source = "aws_live_api" if name == "aws" else "s3compare.io (CC BY 4.0)"
        providers[name] = {
            "storage_classes": prices,
            "source": source,
            "min_storage_duration": MIN_STORAGE_DURATION.get(name, {}),
            "self_hosted": name in ("minio", "ceph"),
        }
    return providers
