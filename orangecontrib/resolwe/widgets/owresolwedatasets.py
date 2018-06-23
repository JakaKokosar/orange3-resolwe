""" OWResolweDataSets """
import os
import sys


from AnyQt.QtWidgets import QLabel,QApplication
from AnyQt.QtCore import QSize

from Orange.data import Table
from Orange.widgets import widget, settings, gui
from Orange.widgets.utils.signals import Output, Input
from Orange.widgets.widget import Msg
from Orange.widgets.data.owdatasets import SizeDelegate, NumericalDelegate

from orangecontrib.resolwe.utils import ResolweHelper
from orangecontrib.resolwe.utils.gui import ResolweDataWidget

from resdk.resources.data import Data


class OWResolweDataSets(widget.OWWidget):
    name = "Resolwe Datasets"
    description = "Load a dataset from resolwe based server"
    icon = "icons/OWResolweDataSets.svg"
    priority = 30

    auto_commit = settings.Setting(True)

    DATA_TYPE = 'singlecell'
    DESCRIPTOR_SCHEMA = 'data_info'

    class Error(widget.OWWidget.Error):
        no_remote_datasets = Msg("Could not fetch dataset list")

    class Warning(widget.OWWidget.Warning):
        only_local_datasets = Msg("pass")

    class Outputs:
        data_object = Output("Data Object", Data)

    class Inputs:
        data_table = Input("Data", Table)

    def __init__(self):
        super().__init__()
        info_box = gui.widgetBox(self.controlArea, "Info")
        self.info_label = QLabel()
        info_box.layout().addWidget(self.info_label)

        self.res = ResolweHelper()
        data_objects = self.res.list_data_objects(self.DATA_TYPE)
        descriptor_schema = self.res.get_descriptor_schema(self.DESCRIPTOR_SCHEMA)

        self.res_widget = ResolweDataWidget(data_objects, descriptor_schema)
        self.res_widget.view.selectionModel().selectionChanged.connect(self.commit)
        self.res_widget.set_target_column(self.res_widget.header.target)

        self.__assign_delegates()

        self.udpdate_info_box()

        self.mainArea.layout().addWidget(self.res_widget)

        self.controlArea.layout().addStretch(10)

        gui.auto_commit(self.controlArea, self, "auto_commit", "&Commit")

        print(os.environ.get('RESOLWE_HOST_URL'))
        print(os.environ.get('RESOLWE_API_USERNAME'))
        print(os.environ.get('RESOLWE_API_PASSWORD'))

    def __assign_delegates(self):
        self.res_widget.view.setItemDelegateForColumn(
            self.res_widget.header.file_size, SizeDelegate(self))

        self.res_widget.view.setItemDelegateForColumn(
            self.res_widget.header.cells, NumericalDelegate(self)
        )
        self.res_widget.view.setItemDelegateForColumn(
            self.res_widget.header.genes, NumericalDelegate(self)
        )

    def udpdate_info_box(self):
        if self.res_widget.data_objects:
            self.info_label.setText('Data objects on server: {}'.format(len(self.res_widget.data_objects)))

    @Inputs.data_table
    def handle_input(self, data):
        # upload file
        self.res.upload_data_table(data)
        # fetch data object and reconstruct data model
        self.res_widget.data_objects = self.res.list_data_objects(self.DATA_TYPE)

    def commit(self):
        sel_data_obj = self.res_widget.selected_data_object()
        assert isinstance(sel_data_obj, Data)
        self.Outputs.data_object.send(sel_data_obj)

    def sizeHint(self):
        return QSize(900, 600)


if __name__ == "__main__":

    def main(args=None):
        if args is None:
            args = sys.argv

        app = QApplication(list(args))
        w = OWResolweDataSets()
        w.show()
        w.raise_()
        rv = app.exec_()
        w.saveSettings()
        w.onDeleteWidget()
        return rv

    sys.exit(main())
