"""HyperLiquid vault scanner.

Fetches the public vault catalog daily, computes risk-adjusted metrics
from per-vault NAV history, and ranks on a quality filter. Read-only —
no funds are deposited or withdrawn here. See `docs/plans/vault-scanner.md`
for design rationale and `docs/hyperliquid-vaults-api.md` for the
verified endpoint shapes.
"""
