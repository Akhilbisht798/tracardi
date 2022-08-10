from tracardi.service.plugin.domain.register import Plugin, Spec, MetaData, Documentation, PortDoc, Form, FormGroup, \
    FormField, FormComponent
from tracardi.service.plugin.domain.result import Result
from tracardi.service.plugin.runner import ActionRunner
from .model.config import Config
from ..credentials import TrelloCredentials
from tracardi.service.storage.driver import storage
from tracardi.process_engine.action.v1.connectors.trello.trello_client import TrelloClient


async def validate(config: dict) -> Config:
    plugin_config = Config(**config)
    credentials = TrelloCredentials(
        **(await storage.driver.resource.load(plugin_config.source.id)).credentials.production
    )
    client = TrelloClient(credentials.api_key, credentials.token)
    list_id = await client.get_list_id(plugin_config.board_url, plugin_config.list_name)
    plugin_config = Config(**plugin_config.dict(exclude={"list_id"}), list_id=list_id)
    return plugin_config


class TrelloCardRemover(ActionRunner):

    @staticmethod
    async def build(**kwargs) -> 'TrelloCardRemover':
        config = Config(**kwargs)
        credentials = TrelloCredentials(
            **(await storage.driver.resource.load(config.source.id)).credentials.production
        )
        client = TrelloClient(credentials.api_key, credentials.token)
        return TrelloCardRemover(client, config)

    def __init__(self, client: TrelloClient, config: Config):
        self._client = client
        self.config = config

    async def run(self, payload: dict, in_edge=None) -> Result:
        dot = self._get_dot_accessor(payload)
        card_name = dot[self.config.card_name]

        try:
            result = await self._client.delete_card(self.config.list_id, card_name)
        except (ConnectionError, ValueError) as e:
            self.console.error(str(e))
            return Result(port="error", value=payload)

        return Result(port="response", value=result)


def register() -> Plugin:
    return Plugin(
        start=False,
        spec=Spec(
            module=__name__,
            className='TrelloCardRemover',
            inputs=["payload"],
            outputs=["response", "error"],
            version='0.6.1',
            license="MIT",
            author="Dawid Kruk",
            manual="trello/delete_trello_card_action"

        ),
        metadata=MetaData(
            name='Remove Trello card',
            desc='Removes card from given list on given board in Trello.',
            icon='trello',
            group=["Trello"],
            documentation=Documentation(
                inputs={
                    "payload": PortDoc(desc="This port takes payload object.")
                },
                outputs={
                    "response": PortDoc(desc="This port returns a response from Trello API."),
                    "error": PortDoc(desc="This port gets triggered if an error occurs.")
                }
            ),
            pro=True
        )
    )
