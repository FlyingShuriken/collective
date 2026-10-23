from uuid import UUID, uuid4

from psycopg import AsyncConnection
from pydantic import ValidationError

from collective.api.dto import DiscoverPool, DiscoveryResult
from collective.api.errors import DomainError
from collective.domain.clock import Clock
from collective.domain.pools import MemberSnapshot, discover_pool, shared_terms
from collective.repositories import pools
from collective.repositories.db import set_context
from collective.repositories.ledger import historical_basis
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.repositories.quotes import confirmed_quotes
from collective.services import needs
from collective.services.permissions import membership


async def discover(
    connection: AsyncConnection,
    subject: UUID,
    business: UUID,
    request: DiscoverPool,
    key: str,
    clock: Clock,
) -> DiscoveryResult:
    await membership(connection, subject, business, owner=True)
    own = await needs.load_need(connection, business, request.need_id)
    await set_context(connection, "coordinator", subject)
    await pools.lock_inputs(connection, own["spec_id"])
    await membership(connection, subject, business, owner=True)
    as_of = clock.now()
    payload = request.model_dump(mode="json")
    old = await previous_mutation(connection, subject, business, "pools/discover", key, payload)
    if old:
        return DiscoveryResult.model_validate(old)
    own = await needs.load_need(connection, business, request.need_id)
    if own["state"] == "reserved" and own["reserved_pool_id"]:
        result = DiscoveryResult(
            state="found",
            need=needs.view(own),
            offer=await pools.own_offer(connection, business, own["reserved_pool_id"], as_of),
        )
    else:
        if own["revision"] != request.expected_revision:
            raise DomainError(
                "STALE_REVISION", "This need changed. Refresh before discovering a pool.", 409
            )
        if own["state"] != "selected":
            raise DomainError(
                "NEED_NOT_SELECTED",
                "Select an uncommitted quantity before discovering a pool.",
                409,
            )
        groups, checks = {}, {}
        for candidate in await pools.candidate_needs(
            connection, own["spec_id"], own["spec_revision"]
        ):
            row = await needs.load_need(connection, candidate["business_id"], candidate["id"])
            if not row["mapping_confirmed"]:
                continue
            try:
                checked = await needs.options(connection, row, as_of)
            except (DomainError, ValidationError):
                continue
            if not checked.stated_quantity_valid:
                continue
            records = await confirmed_quotes(connection, row["business_id"], row["spec_id"])
            quotes = tuple(record.quote for record in records if record.quote is not None)
            history = await historical_basis(connection, needs.snapshot(row))
            checks[str(row["id"])] = checked.model_dump(mode="json")
            for quote in quotes:
                if (
                    not quote.pooled_buyers_permitted
                    or not quote.valid_from <= as_of < quote.valid_until
                ):
                    continue
                if not await pools.supplier_mapping_confirmed(
                    connection,
                    row["business_id"],
                    quote.supplier_id,
                    quote.spec_id,
                    quote.spec_revision,
                ):
                    continue
                member = MemberSnapshot(
                    need=needs.snapshot(row), quote=quote, solo_quotes=quotes, history=history
                )
                groups.setdefault(shared_terms(quote), []).append(member)
        # shortcut: enumerate this small demo; replace the solver before supporting larger cohorts.
        too_large = any(len(group) > 12 for group in groups.values())
        candidates = (
            []
            if too_large
            else [
                result
                for group in groups.values()
                if (result := discover_pool(group, request.need_id, as_of)) is not None
            ]
        )
        selected = min(
            candidates,
            key=lambda result: (
                -len(result.members),
                -sum(m.pooling_saving for m in result.members),
                tuple((str(m.need_id), str(m.quote.id)) for m in result.members),
            ),
            default=None,
        )
        if selected is None:
            reason = "DISCOVERY_REVIEW_REQUIRED" if too_large else "NO_ELIGIBLE_POOL"
            result = DiscoveryResult(
                state="unavailable", need=needs.view(own), failure_codes=(reason,)
            )
        else:
            pool = uuid4()
            chosen_checks = {str(m.need_id): checks[str(m.need_id)] for m in selected.members}
            offers = await pools.reserve(connection, pool, selected, chosen_checks)
            result = DiscoveryResult(
                state="found",
                need=needs.view(await needs.load_need(connection, business, request.need_id)),
                offer=offers[business],
            )
    await save_mutation(
        connection,
        subject,
        business,
        "pools/discover",
        key,
        payload,
        result.model_dump(mode="json"),
    )
    return result
