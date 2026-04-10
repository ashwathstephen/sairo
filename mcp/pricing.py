"""
S3-compatible storage provider pricing tables.

Prices are per GB per month for storage. Egress and API call pricing
varies significantly and is noted but not used in estimates.
"""

# Storage pricing: provider -> storage_class -> price_per_gb_per_month (USD)
STORAGE_PRICING: dict[str, dict[str, float]] = {
    "aws": {
        "standard": 0.023,
        "intelligent_tiering": 0.023,  # frequent access tier
        "standard_ia": 0.0125,
        "one_zone_ia": 0.01,
        "glacier_instant": 0.004,
        "glacier_flexible": 0.0036,
        "glacier_deep_archive": 0.00099,
    },
    "r2": {
        "standard": 0.015,
        # R2 has no egress fees, no storage classes
    },
    "b2": {
        "standard": 0.006,
        # First 10GB free, no minimum storage duration
    },
    "wasabi": {
        "standard": 0.0069,
        # No egress fees, no API fees, 90-day minimum
    },
    "minio": {
        "standard": 0.0,
        # Self-hosted, cost is infrastructure only
    },
    "ceph": {
        "standard": 0.0,
        # Self-hosted
    },
    "leaseweb": {
        "standard": 0.012,
    },
}

# Minimum storage duration (days) before deletion charges apply
MIN_STORAGE_DURATION: dict[str, dict[str, int]] = {
    "aws": {
        "standard": 0,
        "standard_ia": 30,
        "one_zone_ia": 30,
        "glacier_instant": 90,
        "glacier_flexible": 90,
        "glacier_deep_archive": 180,
    },
    "wasabi": {
        "standard": 90,
    },
}

# Region pricing multipliers (relative to us-east-1 = 1.0)
REGION_MULTIPLIERS: dict[str, float] = {
    "us-east-1": 1.0,
    "us-east-2": 1.0,
    "us-west-1": 1.1,
    "us-west-2": 1.0,
    "eu-west-1": 1.08,
    "eu-west-2": 1.08,
    "eu-central-1": 1.08,
    "ap-southeast-1": 1.1,
    "ap-northeast-1": 1.14,
    "ap-south-1": 1.04,
    "sa-east-1": 1.22,
}


def get_storage_price(
    provider: str, storage_class: str = "standard", region: str = "us-east-1"
) -> float:
    """Get price per GB per month for a provider/class/region combo."""
    provider = provider.lower()
    storage_class = storage_class.lower()

    provider_prices = STORAGE_PRICING.get(provider, STORAGE_PRICING["aws"])
    base_price = provider_prices.get(storage_class, provider_prices.get("standard", 0.023))

    # Apply region multiplier (only for AWS currently)
    if provider == "aws":
        multiplier = REGION_MULTIPLIERS.get(region, 1.0)
        base_price *= multiplier

    return base_price


def estimate_monthly_cost(total_bytes: int, provider: str, region: str = "us-east-1") -> dict:
    """
    Estimate monthly storage cost.
    Returns {storage_class: {price_per_gb, total_cost, total_gb}}
    """
    total_gb = total_bytes / (1024 ** 3)
    provider = provider.lower()
    provider_prices = STORAGE_PRICING.get(provider, STORAGE_PRICING["aws"])

    result = {}
    for storage_class, price_per_gb in provider_prices.items():
        # Apply region multiplier
        effective_price = price_per_gb
        if provider == "aws":
            multiplier = REGION_MULTIPLIERS.get(region, 1.0)
            effective_price *= multiplier

        result[storage_class] = {
            "price_per_gb_month": round(effective_price, 6),
            "monthly_cost": round(total_gb * effective_price, 2),
            "annual_cost": round(total_gb * effective_price * 12, 2),
            "total_gb": round(total_gb, 2),
        }

    return result


def calculate_savings(
    total_bytes: int,
    cold_bytes: int,
    provider: str,
    current_class: str = "standard",
    target_class: str = "glacier_instant",
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
