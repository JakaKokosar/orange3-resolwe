""" OWResolweDataObject """
import sys
import threading
import textwrap

from concurrent.futures import Future
from typing import Optional
from resdk import resolwe
from functools import partial

from AnyQt.QtCore import (
    pyqtSlot as Slot
)

from AnyQt.QtWidgets import (
    QLabel, QApplication, QLayout
)

from Orange.data import Table
from Orange.widgets import widget, gui, settings
from Orange.widgets.utils.concurrent import ThreadExecutor, FutureWatcher
from orangecontrib.resolwe.utils import ResolweHelper, ResolweTask


class OWResolweDataObject(widget.OWWidget):
    name = "Resolwe Data Object"
    description = "Resolwe Data object viewer"
    icon = "icons/OWResolweDataObject.svg"
    priority = 20
    want_control_area = True
    want_main_area = False

    auto_commit = settings.Setting(True)

    class Inputs:
        data = widget.Input("Data", resolwe.Data)

    class Outputs:
        data = widget.Output("Data", Table)

    def __init__(self):
        super().__init__()
        self.data_table_object = None       # type: Optional[resolwe.Data]

        # threading
        self._task = None                   # type: Optional[ResolweTask]
        self._executor = ThreadExecutor()

        box = gui.widgetBox(self.controlArea, 'Data Object')
        self._data_obj = QLabel(box)
        box.layout().addWidget(self._data_obj)

        box = gui.widgetBox(self.controlArea, 'Process info')
        self._proc_info = QLabel(box)
        box.layout().addWidget(self._proc_info)

        box = gui.widgetBox(self.controlArea, 'User permissions')
        self._usr_perm = QLabel(box)
        box.layout().addWidget(self._usr_perm)

        self.controlArea.setMinimumWidth(self.controlArea.sizeHint().width())
        self.layout().setSizeConstraint(QLayout.SetFixedSize)

        gui.auto_commit(self.controlArea, self, "auto_commit", "Download data")

        self.res = ResolweHelper()

    @staticmethod
    def pack_table(info):
        return '<table>\n' + "\n".join(
            '<tr><td align="right" width="120">%s:</td>\n'
            '<td width="200">%s</td></tr>\n' % (d, textwrap.shorten(str(v), width=100, placeholder="..."))
            for d, v in info
        ) + "</table>\n"

    @Inputs.data
    def set_data(self, data):
        # type: (Optional[resolwe.Data]) -> None
        self.data_table_object = data

        if self.data_table_object is not None:
            self.setup()

    def handleNewSignals(self):
        self.commit()

    def __setup_data_object_info(self):
        info = self.pack_table((
            ('Id', '{}'.format(self.data_table_object.id)),
            ('Name', '{}'.format(self.data_table_object.name))
        ))
        self._data_obj.setText(info)

    def __setup_proces_info(self):
        info = self.pack_table((
            ('Id', '{}'.format(self.data_table_object.process)),
            ('Name', '{}'.format(self.data_table_object.process_name)),
            ('Category', '{}'.format(self.data_table_object.process_type))
        ))
        self._proc_info.setText(info)

    def __setup_usr_permissions(self):
        if self.data_table_object.current_user_permissions:
            current_usr_perm = self.data_table_object.current_user_permissions[0]
            perms = current_usr_perm.get('permissions', None)
            if perms:
                perms = ','.join(perms)

            info = self.pack_table((
                ('Id', '{}'.format(current_usr_perm.get('id', None))),
                ('Name', '{}'.format(current_usr_perm.get('name', None))),
                ('Type', '{}'.format(current_usr_perm.get('type', None))),
                ('Permissions', '{}'.format(perms))
            ))
            self._usr_perm.setText(info)

    def setup(self):
        self.__setup_data_object_info()
        self.__setup_proces_info()
        self.__setup_usr_permissions()

    def commit(self):
        if not self.data_table_object:
            self.Outputs.data.send(None)
            return
        self.run_task()

    def run_task(self):
        if self._task is not None:
            self.cancel()
        assert self._task is None

        self.progressBarInit()
        func = partial(self.res.download_data_table, self.data_table_object)

        self._task = ResolweTask('download')
        self._task.future = self._executor.submit(func)
        self._task.watcher = FutureWatcher(self._task.future)
        self._task.watcher.done.connect(self.task_finished)

    @Slot(Future, name='Future')
    def task_finished(self, future):
        assert threading.current_thread() == threading.main_thread()
        assert self._task is not None
        assert self._task.future is future
        assert future.done()

        try:
            future_result = future.result()
        except Exception as ex:
            # TODO: raise exceptions
            raise ex
        else:
            if self._task.slug == 'download':
                self.Outputs.data.send(future_result)
        finally:
            self.progressBarFinished()
            self._task = None


if __name__ == "__main__":

    def main(args=None):
        if args is None:
            args = sys.argv

        app = QApplication(list(args))
        w = OWResolweDataObject()
        res = ResolweHelper()
        w.set_data(res.get_object(id=197))
        # w.resetSettings()
        w.show()
        w.raise_()
        rv = app.exec_()
        w.saveSettings()
        w.onDeleteWidget()
        return rv

    sys.exit(main())
