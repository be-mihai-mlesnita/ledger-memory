"""
Phase A tier lattice — named constants and the corrected comparator.

An earlier draft used `sensitivity_tier <= ceiling`, which with tiers numbered
T0=0 (secret) < T1=1 (owner-sensitive) < T2=2 (personal) < T3=3 (shareable)
admitted every tier to a cloud caller at ceiling=3. The correct direction is
`>=`: ceiling means "the most-sensitive tier this caller may read", so a cloud
caller (ceiling=T3) sees only T3, and a local caller (ceiling=T1) sees
{T1, T2, T3}. T0 is never visible via this function, to any ceiling.
"""

TIER_SECRET: int = 0
TIER_OWNER_SENSITIVE: int = 1
TIER_PERSONAL: int = 2
TIER_SHAREABLE: int = 3

TIER_NAMES: dict[int, str] = {
    TIER_SECRET: "secret",
    TIER_OWNER_SENSITIVE: "owner-sensitive",
    TIER_PERSONAL: "personal",
    TIER_SHAREABLE: "shareable",
}


def may_see(row_tier: int, ceiling: int) -> bool:
    """
    Return True iff the row's tier is visible to a caller with the given ceiling.

    Rule: row_tier >= ceiling AND row_tier > TIER_SECRET.
    This means a row is visible iff its tier is at least as permissive as the
    caller's ceiling, AND the row is not T0 (secret). T0 is never visible to
    any ceiling, including when row_tier == ceiling == TIER_SECRET.

    Example: cloud ceiling=TIER_SHAREABLE (3) sees only TIER_SHAREABLE (3);
    local ceiling=TIER_OWNER_SENSITIVE (1) sees TIER_OWNER_SENSITIVE (1),
    TIER_PERSONAL (2), TIER_SHAREABLE (3).
    """
    return row_tier >= ceiling and row_tier > TIER_SECRET
