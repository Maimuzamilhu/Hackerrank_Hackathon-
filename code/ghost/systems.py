"""Canonical system_id for each resource family, as used in the answer steps.

Derived from participant-visible data: the sample answers and system_activity.csv pair each
resource_id prefix with one owning system.
"""
PREFIX_SYSTEM = {
    "account": "account_service",
    "analytics": "analytics_platform",
    "audit_record": "security_service",
    "backup": "backup_service",
    "cache": "cache_service",
    "data_flow": "integration_service",
    "email_delivery": "email_delivery_service",
    "export": "export_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "search": "search_service",
    "support": "support_platform",
    "tombstone": "identity_service",
}

# SQL tables that do not carry an origin_system column.
TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def resource_prefix(resource_id: str) -> str:
    return resource_id.rsplit("_", 1)[0]


def system_for_resource(resource_id: str) -> str | None:
    return PREFIX_SYSTEM.get(resource_prefix(resource_id))


def system_for_table(table: str) -> str | None:
    """customer_profiles_failover_replica -> account_service (longest matching table stem)."""
    for stem in sorted(TABLE_SYSTEM, key=len, reverse=True):
        if table == stem or table.startswith(stem + "_"):
            return TABLE_SYSTEM[stem]
    return None
