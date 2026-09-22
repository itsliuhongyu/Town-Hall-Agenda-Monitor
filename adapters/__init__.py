from adapters.base import AgendaAdapter, AgendaItem
from adapters.townweb import TownWebAdapter
from adapters.boarddocs import BoardDocsAdapter
from adapters.civicclerk import CivicClerkAdapter

REGISTRY: dict[str, AgendaAdapter] = {
    "TownWeb": TownWebAdapter(),
    "BoardDocs": BoardDocsAdapter(),
    "CivicClerk": CivicClerkAdapter(),
}


def get_adapter(platform: str) -> AgendaAdapter | None:
    return REGISTRY.get(platform)
