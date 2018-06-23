""" Utils for resolwe sdk """
import tempfile
import os
import asyncio

from os import environ
from resdk.resolwe import Resolwe
from concurrent.futures import wait

from Orange.data import Table

DEFAULT_URL = 'http://127.0.0.1:8000/'
DEFAULT_USERNAME = 'admin'
DEFAULT_PASSWORD = 'admin123'


def set_resolwe_url(url=DEFAULT_URL):
    environ['RESOLWE_HOST_URL'] = url


def set_resolwe_username(username=DEFAULT_USERNAME):
    environ['RESOLWE_API_USERNAME'] = username


def set_resolwe_password(password=DEFAULT_PASSWORD):
    environ['RESOLWE_API_PASSWORD'] = password


class ResolweTask:
    future = None
    watcher = None
    cancelled = False

    def __init__(self, slug):
        # type: (str) -> None
        self.slug = slug

    def cancel(self):
        self.cancelled = True
        self.future.cancel()
        wait([self.future])


class ResolweHelper:

    def __init__(self):
        self.url = environ.get('RESOLWE_HOST_URL', DEFAULT_URL)
        self.username = environ.get('RESOLWE_API_USERNAME', DEFAULT_USERNAME)
        self.password = environ.get('RESOLWE_API_PASSWORD', DEFAULT_PASSWORD)

        try:
            self.res = Resolwe(self.username, self.password, self.url)
        except Exception:
            # TODO: raise proper exceptions and handle in GUI
            raise

    @staticmethod
    async def check_object_status(data_object):
        while True:
            data_object.update()
            if data_object.status == 'OK' or data_object.status == 'ER':
                return True

            await asyncio.sleep(0.5)

    def run_process(self, slug, **kwargs):

        process = self.res.get_or_run(slug, input={**kwargs})
        if process.status == 'OK':
            return process

        # wait till task is finished
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.wait_for(self.check_object_status(process), timeout=60))
        finally:
            loop.close()

        return process

    def get_json(self, data_object, output_field, json_field=None):
        storage_data = self.res.api.storage(data_object.output[output_field]).get()
        if json_field:
            return storage_data['json'][json_field]
        else:
            return storage_data['json']

    def get_object(self, *args, **kwargs):
        return self.res.data.get(*args, **kwargs)

    def list_data_objects(self, data_type):
        return self.res.data.filter(type='data:table:{}'.format(data_type))

    def get_descriptor_schema(self, slug):
        return self.res.descriptor_schema.get(slug)

    def upload_data_table(self, data_table):

        if not data_table and isinstance(data_table, Table):
            # raise proper warning
            return

        # create temp dir and pickle data.Table object
        with tempfile.TemporaryDirectory() as temp_dir:
            # TODO: What to do if Table is not named?
            file_name = data_table.name + '.pickle'
            file_path = os.path.join(temp_dir, file_name)
            # save Table as pickled object
            data_table.save(file_path)
            # run resolwe upload process
            self.res.run('data-table-upload', input={'src': file_path})

    @staticmethod
    def download_data_table(data_table_object):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_table_object.download(download_dir=temp_dir)
            return Table(os.path.join(temp_dir, data_table_object.name))


