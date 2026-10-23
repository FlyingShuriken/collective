from uuid import UUID

from psycopg import AsyncConnection

from collective.api.dto import Identity, Membership, NetworkAccess
from collective.api.errors import DomainError


async def identity(connection: AsyncConnection, subject: UUID) -> Identity:
    cursor = await connection.execute(
        """
        select b.id as business_id,b.name,m.role,b.locale,
               (m.role='owner' or b.staff_can_view_prices) as can_view_prices
        from collective.business_memberships m join collective.businesses b on b.id=m.business_id
        where m.user_id=%s order by b.name,b.id
    """,
        (subject,),
    )
    memberships = tuple(Membership.model_validate(row) for row in await cursor.fetchall())
    cursor = await connection.execute(
        """
        select n.id as network_id,n.name,'network_admin' as role
        from collective.network_admins a join collective.networks n on n.id=a.network_id
        where a.user_id=%s order by n.name,n.id
    """,
        (subject,),
    )
    return Identity(
        subject=subject,
        memberships=memberships,
        networks=tuple(NetworkAccess.model_validate(row) for row in await cursor.fetchall()),
    )


async def membership(
    connection: AsyncConnection,
    subject: UUID,
    business_id: UUID,
    *,
    owner: bool = False,
) -> Membership:
    cursor = await connection.execute(
        """
        select b.id as business_id,b.name,m.role,b.locale,
               (m.role='owner' or b.staff_can_view_prices) as can_view_prices
        from collective.business_memberships m join collective.businesses b on b.id=m.business_id
        where m.user_id=%s and b.id=%s
    """,
        (subject, business_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    result = Membership.model_validate(row)
    if owner and result.role != "owner":
        raise DomainError("OWNER_REQUIRED", "This action requires the business owner.", 403)
    return result
