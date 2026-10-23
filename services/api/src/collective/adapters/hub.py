from collective.api.dto import HubService
from collective.api.errors import DomainError

SERVICES = (HubService(name="collective", description="Restocking workspace and human approvals."),)


def service(name: str) -> HubService:
    for item in SERVICES:
        if item.name == name:
            return item
    raise DomainError("NOT_FOUND", "Resource not found.", 404)
