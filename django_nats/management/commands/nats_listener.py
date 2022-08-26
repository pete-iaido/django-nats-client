import asyncio
import json

from asgiref.sync import SyncToAsync
from django.conf import settings
from django.core.management import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections
from nats.aio.client import Client
from nats.aio.client import Msg
from nats.aio.errors import ErrNoServers
from nats.aio.errors import ErrTimeout

from django_nats.registry import default_registry


class DatabaseSyncToAsync(SyncToAsync):
    """
    SyncToAsync version that cleans up old database connections when it exits.
    """

    def thread_handler(self, loop, *args, **kwargs):
        close_old_connections()
        try:
            return super().thread_handler(loop, *args, **kwargs)
        finally:
            close_old_connections()


# The class is TitleCased, but we want to encourage use as a callable/decorator
database_sync_to_async = DatabaseSyncToAsync


class Command(BaseCommand):
    nats = Client()

    def handle(self, *args, **options):
        loop = asyncio.get_event_loop()
        print('** Initializing Loop')
        try:
            asyncio.ensure_future(self.nats_coroutine())
            loop.run_forever()
        except KeyboardInterrupt:
            loop.run_until_complete(self.clean())
        finally:
            loop.close()

    async def nats_coroutine(self):
        try:
            await self.nats.connect(**settings.NATS_OPTIONS)
            print('** Connected to NATS server')
        except (ErrNoServers, ErrTimeout) as e:
            raise e

        async def callback(msg: Msg):
            reply = msg.reply
            data = msg.data.decode()
            print(f'Received a message on "{msg.subject} {reply}": {data}')
            await self.nats_handler(reply, data)

        for subject in default_registry.subjects:
            await self.nats.subscribe(subject, cb=callback)

    async def clean(self):
        await self.nats.close()

    async def nats_handler(self, reply, body):
        data = json.loads(body)

        name = data['name']
        args = data['args']
        kwargs = data['kwargs']

        func = default_registry.registry.get(name)
        if func is None:
            print(f'No function found for "{name}"')

        func = database_sync_to_async(func)
        r = await func(*args, **kwargs)
        await self.nats.publish(reply, json.dumps({'result': r}, cls=DjangoJSONEncoder).encode())