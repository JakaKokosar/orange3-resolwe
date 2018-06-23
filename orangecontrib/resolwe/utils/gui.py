""" PyQt components for resolwe add-on"""
from AnyQt.QtCore import Qt
from AnyQt.QtWidgets import QWidget, QTreeView, QVBoxLayout
from AnyQt.QtGui import QStandardItem, QStandardItemModel


from Orange.widgets.data.owdatasets import variable_icon
from resdk.resources.data import Data
from collections import namedtuple


class ResolweDataWidget(QWidget):

    def __init__(self, data_objects, descriptor_schema, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ow = kwargs.get('parent', None)

        self._data_objects = data_objects
        self.descriptor_schema = descriptor_schema
        self.header_schema = None
        self.header = None

        # set layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.view = QTreeView()
        self.view.setSortingEnabled(False)
        self.view.setAlternatingRowColors(True)
        self.view.setEditTriggers(QTreeView.NoEditTriggers)
        self.view.setSelectionMode(QTreeView.SingleSelection)

        self.model = QStandardItemModel()
        self.display_data_objects()

        self.layout().addWidget(self.view)

    def __set_header_values(self):
        if self.header_schema:
            labels = [val.get('label', '?') for val in self.header_schema]
            self.model.setHorizontalHeaderLabels(labels)

    def __create_row(self, obj):
        row_items = []
        tabular_data = obj.descriptor.get('tabular', None)
        output_data = obj.output.get('table', None)

        # TODO: refactor this. Use file_name and size from obj.output instead of desc. schema
        for schema_value in self.header_schema:
            item = QStandardItem()
            schema_key = schema_value['name']
            data_info = tabular_data.get(schema_key, '?') if tabular_data else '?'

            if schema_key == 'file_name' and data_info == '?':
                data_info = output_data.get('file', '?') if output_data else '?'
            elif schema_key == 'file_size' and data_info == '?':
                data_info = output_data.get('size', '?') if output_data else '?'

            item.setData(data_info, Qt.DisplayRole)
            row_items.append(item)

        return row_items

    def __populate_data_model(self):
        if self.model:
            self.model.clear()
            for data_object in self.data_objects:
                self.model.appendRow(self.__create_row(data_object))

    def __parse_description_schema(self):
        self.header_schema = []

        if self.descriptor_schema:
            for schema_value in self.descriptor_schema.schema:
                if schema_value['name'] == 'tabular':
                    [self.header_schema.append(value) for value in schema_value['group']]

        if self.header_schema:
            keys = [val.get('name', '?') for val in self.header_schema]
            header_index = namedtuple('header_index', [label for label in keys])
            self.header = header_index(*[index for index, _ in enumerate(keys)])

    @property
    def data_objects(self):
        return self._data_objects

    @data_objects.setter
    def data_objects(self, data_objects):
        self._data_objects = data_objects
        self.display_data_objects()

    def display_data_objects(self):
        self.__parse_description_schema()
        self.__populate_data_model()
        self.__set_header_values()
        self.view.setModel(self.model)

    def set_target_column(self, target_column):
        # type: (int) -> None

        for row in range(self.model.rowCount()):
            item = self.model.item(row, target_column)
            item_data = item.data(role=Qt.DisplayRole)
            if item_data:
                item.setIcon(variable_icon(item_data))

    def selected_data_object(self):
        # type: () -> Data
        rows = self.view.selectionModel().selectedRows()
        assert 0 <= len(rows) <= 1
        sel_row_index = rows[0].row() if rows else None

        obj_range = range(len(self._data_objects))
        assert sel_row_index in obj_range

        try:
            return self._data_objects[sel_row_index]
        except IndexError:
            # can this happen? self._data_objects can't
            # be empty if model is constructed
            pass
