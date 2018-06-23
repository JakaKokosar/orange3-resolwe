""" OWResolwetSNE """
import os.path
import re
import sys
import threading
import numpy as np

from joblib.memory import Memory
from typing import Optional, Sequence, Tuple, Dict

from AnyQt.QtWidgets import QFormLayout, QApplication
from AnyQt.QtGui import QPainter
from AnyQt.QtCore import Qt, pyqtSignal as Signal, pyqtSlot as Slot

import Orange.data
from Orange.data import Domain, Table, ContinuousVariable, DiscreteVariable
import Orange.projection
import Orange.distance
import Orange.misc


from Orange.misc.environ import cache_dir
from Orange.widgets import gui, settings
from Orange.widgets.settings import SettingProvider
from Orange.widgets.utils.sql import check_sql_input
from Orange.canvas import report
from Orange.widgets.visualize.owscatterplotgraph import OWScatterPlotGraph, InteractiveViewBox
from Orange.widgets.widget import Msg, OWWidget, Input, Output
from Orange.widgets.utils.annotated_data import (
    create_annotated_table, create_groups_table, ANNOTATED_DATA_SIGNAL_NAME)


from Orange.widgets.utils.concurrent import ThreadExecutor, FutureWatcher
from functools import partial
from concurrent.futures import Future, CancelledError
from resdk import resolwe
from orangecontrib.resolwe.utils import ResolweHelper, ResolweTask


class MDSInteractiveViewBox(InteractiveViewBox):
    def _dragtip_pos(self):
        return 10, 10


class OWMDSGraph(OWScatterPlotGraph):
    jitter_size = settings.Setting(0)

    def __init__(self, scatter_widget, parent=None, name="None", view_box=None):
        super().__init__(scatter_widget, parent=parent, _=name,
                         view_box=view_box)
        for axis_loc in ["left", "bottom"]:
            self.plot_widget.hideAxis(axis_loc)

    def update_data(self, attr_x, attr_y, reset_view=True):
        super().update_data(attr_x, attr_y, reset_view=reset_view)
        for axis in ["left", "bottom"]:
            self.plot_widget.hideAxis(axis)
        self.plot_widget.setAspectLocked(True, 1)

    def compute_sizes(self):
        def scale(a):
            dmin, dmax = np.nanmin(a), np.nanmax(a)
            if dmax - dmin > 0:
                return (a - dmin) / (dmax - dmin)
            else:
                return np.zeros_like(a)

        self.master.Information.missing_size.clear()
        if self.attr_size is None:
            size_data = np.full((self.n_points,), self.point_width,
                                dtype=float)
        else:
            size_data = \
                self.MinShapeSize + \
                self.scaled_data.get_column_view(self.attr_size)[0][self.valid_data] * \
                self.point_width
        nans = np.isnan(size_data)
        if np.any(nans):
            size_data[nans] = self.MinShapeSize - 2
            self.master.Information.missing_size(self.attr_size)
        return size_data


class OWResolwetSNE(OWWidget):
    name = "t-SNE"
    description = "Two-dimensional data projection with t-SNE."
    icon = "icons/OWResolwetSNE.svg"
    priority = 50

    class Inputs:
        data = Input("Data", resolwe.Data, default=True)

    class Outputs:
        selected_data = Output("Selected Data", resolwe.Data, default=True)

    settings_version = 2

    #: Runtime state
    Running, Finished, Waiting = 1, 2, 3

    settingsHandler = settings.DomainContextHandler()

    max_iter = settings.Setting(300)
    perplexity = settings.Setting(30)
    pca_components = settings.Setting(20)

    # output embedding role.
    NoRole, AttrRole, AddAttrRole, MetaRole = 0, 1, 2, 3

    auto_commit = settings.Setting(True)

    selection_indices = settings.Setting(None, schema_only=True)

    legend_anchor = settings.Setting(((1, 0), (1, 0)))

    graph = SettingProvider(OWMDSGraph)

    jitter_sizes = [0, 0.1, 0.5, 1, 2, 3, 4, 5, 7, 10]

    graph_name = "graph.plot_widget.plotItem"

    class Error(OWWidget.Error):
        not_enough_rows = Msg("Input data needs at least 2 rows")
        constant_data = Msg("Input data is constant")
        no_attributes = Msg("Data has no attributes")
        out_of_memory = Msg("Out of memory")
        optimization_error = Msg("Error during optimization\n{}")

    def __init__(self):
        super().__init__()
        #: Effective data used for plot styling/annotations.
        self.data = None  # type: Optional[Orange.data.Table]
        #: Input subset data table
        self.subset_data = None  # type: Optional[Orange.data.Table]
        #: Input data table
        self.signal_data = None

        # resolwe variables
        self.data_table_object = None  # type: Optional[resolwe.Data]

        self._tsne_slug = 't-sne'
        self._tsne_selection_slug = 't-sne-selection'
        self._embedding_data_object = None
        self._embedding = None
        self._embedding_clas_var = None
        self.variable_x = ContinuousVariable("tsne-x")
        self.variable_y = ContinuousVariable("tsne-y")

        # threading
        self._task = None  # type: Optional[ResolweTask]
        self._executor = ThreadExecutor()

        self.res = ResolweHelper()

        self._subset_mask = None  # type: Optional[np.ndarray]
        self._invalidated = False
        self.pca_data = None
        self._curve = None
        self._data_metas = None

        self.variable_x = ContinuousVariable("tsne-x")
        self.variable_y = ContinuousVariable("tsne-y")

        self.__update_loop = None

        self.__in_next_step = False
        self.__draw_similar_pairs = False

        box = gui.vBox(self.controlArea, "t-SNE")
        form = QFormLayout(
            labelAlignment=Qt.AlignLeft,
            formAlignment=Qt.AlignLeft,
            fieldGrowthPolicy=QFormLayout.AllNonFixedFieldsGrow,
            verticalSpacing=10
        )

        form.addRow(
            "Max iterations:",
            gui.spin(box, self, "max_iter", 250, 2000, step=50))

        form.addRow(
            "Perplexity:",
            gui.spin(box, self, "perplexity", 1, 100, step=1))

        box.layout().addLayout(form)

        gui.separator(box, 10)
        self.runbutton = gui.button(box, self, "Run", callback=self._run_embeding)

        box = gui.vBox(self.controlArea, "PCA Preprocessing")
        gui.hSlider(box, self, 'pca_components', label="Components: ",
                    minValue=2, maxValue=50, step=1) #, callback=self._initialize)

        box = gui.vBox(self.mainArea, True, margin=0)
        self.graph = OWMDSGraph(self, box, "MDSGraph", view_box=MDSInteractiveViewBox)
        box.layout().addWidget(self.graph.plot_widget)
        self.plot = self.graph.plot_widget

        g = self.graph.gui
        box = g.point_properties_box(self.controlArea)
        self.models = g.points_models
        # Because sc data frequently has many genes,
        # showing all attributes in combo boxes can cause problems
        # QUICKFIX: Remove a separator and attributes from order
        # (leaving just the class and metas)
        for model in self.models:
            model.order = model.order[:-2]

        g.add_widgets(ids=[g.JitterSizeSlider], widget=box)

        box = gui.vBox(self.controlArea, "Plot Properties")
        g.add_widgets([g.ShowLegend,
                       g.ToolTipShowsAll,
                       g.ClassDensity,
                       g.LabelOnlySelected], box)

        self.controlArea.layout().addStretch(100)
        self.icons = gui.attributeIconDict

        palette = self.graph.plot_widget.palette()
        self.graph.set_palette(palette)

        gui.rubber(self.controlArea)

        self.graph.box_zoom_select(self.controlArea)

        gui.auto_commit(self.controlArea, self, "auto_commit", "Send Selection",
                        "Send Automatically")

        self.plot.getPlotItem().hideButtons()
        self.plot.setRenderHint(QPainter.Antialiasing)

        self.graph.jitter_continuous = True
        # self._initialize()

    def update_colors(self):
        pass

    def update_density(self):
        self.update_graph(reset_view=False)

    def update_regression_line(self):
        self.update_graph(reset_view=False)

    def prepare_data(self):
        pass

    def update_graph(self, reset_view=True, **_):
        self.graph.zoomStack = []
        if self.graph.data is None:
            return
        self.graph.update_data(self.variable_x, self.variable_y, reset_view=True)

    def reset_graph_data(self, *_):
        if self.data is not None:
            self.graph.rescale_data()
            self.update_graph()

    def selection_changed(self):
        if self._task:
            self.cancel(clear_state=False)
            self._task = None
            self._executor = ThreadExecutor()

        self.commit()

    def _clear_plot(self):
        self.graph.plot_widget.clear()

    def _clear_state(self):
        self._clear_plot()
        self.graph.new_data(None)
        self._embedding_data_object = None
        self._embedding = None
        self._embedding_clas_var = None
        self._task = None
        self._executor = ThreadExecutor()

    def cancel(self, clear_state=True):
        """Cancel the current task (if any)."""

        if self._task is not None:
            self._executor.shutdown(wait=False)
            self.runbutton.setText('Run')
            self.progressBarFinished()
            if clear_state:
                self._clear_state()

    def run_task(self, slug, func):
        if self._task is not None:
            try:
                self.cancel()
            except CancelledError as e:
                print(e)
        assert self._task is None

        self.progressBarInit()

        self._task = ResolweTask(slug)
        self._task.future = self._executor.submit(func)
        self._task.watcher = FutureWatcher(self._task.future)
        self._task.watcher.finished.connect(self.task_finished)

    @Slot(Future, name='Finished')
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
            if self._task.slug == self._tsne_slug:
                self._embedding_data_object = future_result
                self._embedding_clas_var = self.res.get_json(self._embedding_data_object, 'class_var')
                self._embedding = np.array(self.res.get_json(self._embedding_data_object,
                                                             'embedding_json',
                                                             'embedding'))
                self._setup_plot()

            if self._task.slug == self._tsne_selection_slug:
                print(future_result)
                self.Outputs.selected_data.send(future_result)

        finally:
            self.progressBarFinished()
            self.runbutton.setText('Start')
            self._task = None

    @Inputs.data
    def set_data(self, data):
        # type: (Optional[resolwe.Data]) -> None
        if data:
            self.data_table_object = data
            self._run_embeding()

    def _run_embeding(self):
        if self._task:
            self.cancel()
            return

        if self._task is None:
            inputs = {
                'data_table': self.data_table_object,
                'pca_components': self.pca_components,
                'perplexity': self.perplexity,
                'iterations': self.max_iter
            }
            if self._embedding is not None and self._embedding_data_object is not None:
                inputs['init'] = self._embedding_data_object

            func = partial(self.res.run_process,
                           self._tsne_slug,
                           **inputs)

            # move filter process in thread
            self.run_task(self._tsne_slug, func)
            self.runbutton.setText('Stop')

    def _setup_plot(self):
        class_var = DiscreteVariable(self._embedding_clas_var['name'], values=self._embedding_clas_var['values'])
        y_data = self._embedding_clas_var['y_data']
        data = np.c_[self._embedding, y_data]

        plot_data = Table(
            Domain([self.variable_x, self.variable_y], class_vars=class_var), data
        )

        domain = plot_data and len(plot_data) and plot_data.domain or None
        for model in self.models:
            model.set_domain(domain)
        self.graph.attr_color = plot_data.domain.class_var if domain else None
        self.graph.attr_shape = None
        self.graph.attr_size = None
        self.graph.attr_label = None

        self.graph.new_data(plot_data)
        self.graph.update_data(self.variable_x, self.variable_y, True)

    def commit(self):
        selection = self.graph.get_selection()
        if self._embedding_data_object is not None and selection is not None:
            inputs = {'data_table': self.data_table_object,
                      'embedding': self._embedding_data_object,
                      'selection': selection.tolist(),
                      'x_tsne_var': self.variable_x.name,
                      'y_tsne_var': self.variable_y.name}

            func = partial(self.res.run_process,
                           self._tsne_selection_slug,
                           **inputs)

            self.run_task(self._tsne_selection_slug, func)

        self.Outputs.selected_data.send(None)

    def onDeleteWidget(self):
        super().onDeleteWidget()
        self._clear_plot()
        self._clear_state()

    def send_report(self):
        if self.data is None:
            return

        def name(var):
            return var and var.name

        caption = report.render_items_vert((
            ("Color", name(self.graph.attr_color)),
            ("Label", name(self.graph.attr_label)),
            ("Shape", name(self.graph.attr_shape)),
            ("Size", name(self.graph.attr_size)),
            ("Jittering", self.graph.jitter_size != 0 and "{} %".format(self.graph.jitter_size))))
        self.report_plot()
        if caption:
            self.report_caption(caption)


def main(argv=None):
    if argv is None:
        argv = sys.argv
    import gc
    app = QApplication(list(argv))
    argv = app.arguments()
    if len(argv) > 1:
        filename = argv[1]
    else:
        filename = "iris"

    data = Orange.data.Table(filename)
    w = OWResolwetSNE()
    w.set_data(data)
    w.handleNewSignals()

    w.show()
    w.raise_()
    rval = app.exec_()

    w.set_data(None)
    w.handleNewSignals()

    w.saveSettings()
    w.onDeleteWidget()
    w.deleteLater()
    del w
    gc.collect()
    app.processEvents()
    return rval

if __name__ == "__main__":
    sys.exit(main())
