"""
MCP Prompts: guided analysis workflows the user can trigger.

Each prompt generates a structured instruction set that tells the AI
exactly which tools to call and how to synthesize the results into
an actionable report.
"""


def register(mcp):
    """Register all prompts with the MCP server."""

    @mcp.prompt(
        name="storage-audit",
        description=(
            "Run a comprehensive storage audit on a bucket. "
            "Analyzes folder structure, file types, size distribution, data age, "
            "duplicates, and cost optimization opportunities. "
            "Produces a structured report with findings and recommendations."
        ),
    )
    async def storage_audit(bucket: str) -> str:
        return f"""Please perform a comprehensive storage audit of the bucket "{bucket}".

Follow these steps in order:

1. **Overview**: Call `list_buckets` to confirm the bucket exists and get its current stats.

2. **Storage Breakdown**: Call `get_storage_breakdown` for bucket "{bucket}" to see where storage is concentrated.

3. **File Types**: Call `get_file_type_distribution` for bucket "{bucket}" to understand what kinds of files are stored.

4. **Size Distribution**: Call `get_size_distribution` for bucket "{bucket}" to see the file size patterns.

5. **Age Analysis**: Call `get_age_distribution` for bucket "{bucket}" to understand data freshness.

6. **Duplicates**: Call `find_duplicates` for bucket "{bucket}" to find wasted storage.

7. **Cost Optimization**: Call `suggest_lifecycle_rules` for bucket "{bucket}" to find savings opportunities.

After gathering all data, synthesize the findings into a structured audit report with:
- **Executive Summary**: 2-3 sentences on the bucket's overall health
- **Key Findings**: Bullet points of the most important discoveries
- **Storage Distribution**: Which folders and file types dominate
- **Data Freshness**: How old the data is and what's actively changing
- **Waste & Duplicates**: How much space could be reclaimed
- **Cost Recommendations**: Specific actions to reduce costs, with estimated savings
- **Action Items**: Prioritized list of recommended changes

Keep the report concise and actionable. Focus on what matters most."""

    @mcp.prompt(
        name="cost-optimization",
        description=(
            "Analyze storage costs and find savings opportunities. "
            "Estimates current costs, finds cold data, identifies duplicates, "
            "and recommends lifecycle rules with dollar savings estimates."
        ),
    )
    async def cost_optimization(bucket: str, provider: str = "aws") -> str:
        return f"""Please analyze the storage costs for bucket "{bucket}" on {provider.upper()} and find all possible savings.

Follow these steps:

1. **Current Costs**: Call `estimate_storage_cost` for bucket "{bucket}" with provider "{provider}" to see current and alternative pricing.

2. **Cold Data**: Call `find_cold_data` for bucket "{bucket}" with older_than_days=90 to find archival candidates.

3. **Duplicates**: Call `find_duplicates` for bucket "{bucket}" to find redundant copies.

4. **Lifecycle Rules**: Call `suggest_lifecycle_rules` for bucket "{bucket}" with provider "{provider}" for specific recommendations.

5. **Age Profile**: Call `get_age_distribution` for bucket "{bucket}" to understand the full age picture.

Present the results as a cost optimization report with:
- **Current Monthly Cost**: What the bucket costs today on {provider.upper()}
- **Quick Wins**: Savings from removing duplicates and empty files
- **Storage Class Optimization**: Savings from moving cold data to cheaper tiers
- **Lifecycle Policy Recommendations**: Specific rules to implement with estimated savings
- **Total Potential Savings**: Monthly and annual dollar amounts
- **Implementation Steps**: What to do first, second, third

Be specific with dollar amounts. The user wants to know exactly how much they can save."""

    @mcp.prompt(
        name="data-quality-check",
        description=(
            "Check the health and freshness of data pipelines writing to a bucket. "
            "Detects stale partitions, broken naming patterns, and unexpected changes."
        ),
    )
    async def data_quality_check(bucket: str, prefix: str = "") -> str:
        prefix_note = f' within prefix "{prefix}"' if prefix else ""
        return f"""Please check the data quality and pipeline health for bucket "{bucket}"{prefix_note}.

Follow these steps:

1. **Freshness**: Call `detect_data_freshness` for bucket "{bucket}"{f' with prefix "{prefix}"' if prefix else ''} to see which folders are active vs stale.

2. **Structure**: Call `analyze_prefix_structure` for bucket "{bucket}"{f' with prefix "{prefix}"' if prefix else ''} to understand the naming patterns and organization.

3. **Recent Changes**: Call `compare_snapshots` for bucket "{bucket}" with days_ago=7 to see what changed recently.

4. **Folder Overview**: Call `list_folders` for bucket "{bucket}"{f' with prefix "{prefix}"' if prefix else ''} to see the current state.

5. **Index Status**: Call `get_crawl_status` for bucket "{bucket}" to ensure the data is fresh.

Present the results as a data quality report with:
- **Pipeline Status**: Which data sources are actively writing (fresh) vs. stale
- **Structure Assessment**: Is the naming consistent? Are there expected patterns (Hive partitions, date paths)?
- **Anomalies**: Anything unexpected — sudden growth, missing partitions, broken patterns
- **Recent Activity**: What changed in the last 7 days and whether it looks normal
- **Recommendations**: Any issues that need attention

Flag anything that looks like a broken pipeline or data quality issue."""

    @mcp.prompt(
        name="incident-investigation",
        description=(
            "Investigate a storage incident — sudden growth, unexpected changes, "
            "or anomalies. Builds a timeline and identifies likely causes."
        ),
    )
    async def incident_investigation(bucket: str, timeframe_days: int = 7) -> str:
        return f"""Please investigate what happened in bucket "{bucket}" over the last {timeframe_days} days. Something may have changed unexpectedly.

Follow these steps:

1. **Timeline**: Call `get_storage_trends` for bucket "{bucket}" with days={timeframe_days} to see the growth timeline.

2. **What Changed**: Call `compare_snapshots` for bucket "{bucket}" with days_ago={timeframe_days} to see the delta.

3. **Biggest Files**: Call `get_top_objects` for bucket "{bucket}" with sort_by="date" and limit=25 to see the most recent additions.

4. **Storage Breakdown**: Call `get_storage_breakdown` for bucket "{bucket}" to see which folders are involved.

5. **Audit Trail**: Call `get_audit_log` with bucket="{bucket}" and limit=100 to see who did what.

6. **Folder Freshness**: Call `detect_data_freshness` for bucket "{bucket}" to see which folders changed.

Present the results as an incident investigation report with:
- **Summary**: What happened in 2-3 sentences
- **Timeline**: Day-by-day account of storage changes
- **Root Cause**: Which folders/files drove the change and who/what created them
- **Impact**: How much storage was added/removed and the cost impact
- **Audit Trail**: Key user actions from the audit log
- **Recommendations**: What to do next — clean up, set limits, add alerts, etc.

Focus on answering: What happened, when, who did it, and what should we do about it."""
