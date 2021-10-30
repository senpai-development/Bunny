from asyncio import get_event_loop
from logging import Logger, basicConfig, getLogger
from typing import Any, Callable, Coroutine, List, Optional, Union

from .api.cache import Cache, Item
from .api.error import InteractionException, JSONException
from .api.gateway import WebSocket
from .api.http import HTTPClient
from .api.models.guild import Guild
from .api.models.intents import Intents
from .api.models.team import Application
from .base import Data
from .enums import ApplicationCommandType
from .models.command import ApplicationCommand, Option

# TODO: Find a better way to call on the cache for
# storing the token. Yes, this is a piss poor approach,
# but i'm on a time crunch to make the caching work.
cache = Cache()

basicConfig(level=Data.LOGGER)
log: Logger = getLogger("client")


class Client:
    """
    A class representing the client connection to senpai's gateway and API via. WebSocket and HTTP.

    :ivar asyncio.AbstractEventLoop loop: The main overall asynchronous coroutine loop in effect.
    :ivar bunny.api.dispatch.Listener listener: An instance of :class:`bunny.api.dispatch.Listener`.
    :ivar typing.Optional[typing.Union[bunny.api.models.intents.Intents, typing.List[bunny.api.models.intentsIntents]]] intents: The application's intents as :class:`bunny.api.models.Intents`.
    :ivar bunny.api.http.Request http: An instance of :class:`bunny.api.http.Request`.
    :ivar bunny.api.gateway.WebSocket websocket: An instance of :class:`bunny.api.gateway.WebSocket`.
    :ivar str token: The application token.
    """

    def __init__(
        self, token: str, intents: Optional[Union[Intents, List[Intents]]] = Intents.DEFAULT
    ) -> None:
        """
        :param token: The token of the application for authentication and connection.
        :type token: str
        :param intents: The intents you wish to pass through the client. Defaults to :meth:`bunny.api.models.Intents.DEFAULT` or ``513``.
        :type intents: typing.Optional[typing.Union[bunny.api.models.Intents, typing.List[Intents]]]
        :return: None
        """
        if isinstance(intents, list):
            for intent in intents:
                self.intents |= intent
        else:
            self.intents = intents

        self.loop = get_event_loop()
        self.http = HTTPClient(token)
        self.websocket = WebSocket(intents=self.intents)
        self.me = None
        self.token = token
        cache.token = token
        # TODO: Code an internal ready state check for caching reasons.

        if not self.me:
            data = self.loop.run_until_complete(self.http.get_self())
            self.me = Application(**data)

        self.websocket.dispatch.register(self.raw_socket_create)
        self.websocket.dispatch.register(self.raw_guild_create, "on_guild_create")

    async def login(self, token: str) -> None:
        """
        Makes a login with the senpai API.

        :param token: The application token needed for authorization.
        :type token: str
        :return: None
        """
        while not self.websocket.closed:
            await self.websocket.connect(token)

    def start(self) -> None:
        """Starts the client session."""
        self.synchronize_commands()
        self.loop.run_until_complete(self.login(self.token))

    def synchronize_commands(self, name: Optional[str] = None) -> None:
        # TODO: Doctype what this does.
        commands = self.loop.run_until_complete(self.http.get_application_command(self.me.id))
        change: list = []

        for command in commands:
            _command: Optional[Item] = cache.bunny.get(command["id"])
            if _command:
                if ApplicationCommand(**command) == _command:
                    log.warning(f"Detected change to command ID {command.id}.")
                    change.append(command)
            else:
                cache.bunny.add(Item(command["id"], ApplicationCommand(**command)))

        for command in change:
            log.debug(f"Updated command {command.id}.")
            self.http.edit_application_command(
                application_id=self.me.id,
                data=command["data"],
                command_id=command["id"],
                guild_id=command.get("guild_id"),
            )
            cache.bunny.add(Item(command["id"], ApplicationCommand(**command)))

    def event(self, coro: Coroutine) -> Callable[..., Any]:
        """
        A decorator for listening to dispatched events from the
        gateway.

        :return: typing.Callable[..., typing.Any]
        """
        self.websocket.dispatch.register(
            coro, name=coro.__name__ if coro.__name__.startswith("on") else "on_interaction_create"
        )
        return coro

    def command(
        self,
        *,
        type: Optional[Union[str, int, ApplicationCommandType]] = ApplicationCommandType.CHAT_INPUT,
        name: Optional[str] = None,
        description: Optional[str] = None,
        scope: Optional[Union[int, Guild, List[int], List[Guild]]] = None,
        options: Optional[List[Option]] = None,
        default_permission: Optional[bool] = None
        # permissions: Optional[List[Permission]] = None,
    ) -> Callable[..., Any]:
        """
        A decorator for registering an application command to the senpai API,
        as well as being able to listen for ``INTERACTION_CREATE`` dispatched
        gateway events.

        :param type: The type of application command. Defaults to :meth:`bunny.enums.ApplicationCommandType.CHAT_INPUT` or ``1``.
        :type type: typing.Optional[typing.Union[str, int, bunny.enums.ApplicationCommandType]]
        :param name: The name of the application command. This *is* required but kept optional to follow kwarg rules.
        :type name: typing.Optional[str]
        :param description: The description of the application command. This should be left blank if you are not using ``CHAT_INPUT``.
        :type description: typing.Optional[str]
        :param scope: The "scope"/applicable guilds the application command applies to.
        :type scope: typing.Optional[typing.Union[int, bunny.api.models.guild.Guild, typing.List[int], typing.List[bunny.api.models.guild.Guild]]]
        :param options: The "arguments"/options of an application command. This should bel eft blank if you are not using ``CHAT_INPUT``.
        :type options: typing.Optional[typing.List[bunny.models.command.Option]]
        :param default_permission: The default permission of accessibility for the application command. Defaults to ``True``.
        :type default_permission: typing.Optional[bool]
        :return: typing.Callable[..., typing.Any]
        """
        if not name:
            raise Exception("Command must have a name!")

        if name and not description:
            raise Exception("Chat-input commands must have a description!")

        def decorator(coro: Coroutine) -> Any:
            if "ctx" not in coro.__code__.co_varnames:
                raise InteractionException(11)

            _description: str = "" if description is None else description
            _options: list = [] if options is None else options
            _default_permission: bool = True if default_permission is None else default_permission
            # _permissions: list = [] if permissions is None else permissions
            _scope: list = []

            if isinstance(scope, list):
                if all(isinstance(x, Guild) for x in scope):
                    _scope.append(guild.id for guild in scope)
                elif all(isinstance(x, int) for x in scope):
                    _scope.append(guild for guild in scope)
            else:
                _scope.append(scope)

            for guild in _scope:
                payload: ApplicationCommand = ApplicationCommand(
                    type=type.value if isinstance(type, ApplicationCommandType) else type,
                    name=name,
                    description=_description,
                    options=_options,
                    default_permission=_default_permission,
                )

                request = self.loop.run_until_complete(
                    self.http.create_application_command(
                        self.me.id, data=payload._json, guild_id=guild
                    )
                )

                if request.get("code"):
                    raise JSONException(request["code"])  # TODO: work on this pls

                for interaction in cache.bunny.values:
                    if interaction.values[interaction].value.name == name:
                        self.synchronize_commands(name)
                        # TODO: make a call to our internal sync method instead of an exception.
                    else:
                        cache.bunny.add(Item(id=request["application_id"], value=payload))

            return self.event(coro)

        return decorator

    async def raw_socket_create(self, data: dict) -> None:
        # TODO: doctype what this does
        return data

    async def raw_guild_create(self, guild) -> None:
        """
        This is an internal function that caches the guild creates on ready.
        :param guild: Guild object.
        :return: None.
        """
        cache.guilds.add(Item(id=guild.id, value=guild))
