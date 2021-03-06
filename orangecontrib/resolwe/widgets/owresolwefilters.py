""" OWResolweFilter """
import sys
import threading
import pyqtgraph as pg
import numpy as np
import Orange.widgets.utils.plot.owpalette

from functools import partial
from concurrent.futures import Future
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Optional, Sequence, Tuple, Dict
from scipy import stats
from resdk import resolwe

from AnyQt.QtCore import (
    Qt, QSize, QPointF, QRectF, QLineF, pyqtSignal as Signal, pyqtSlot as Slot
)
from AnyQt.QtGui import (
    QPainter, QPolygonF, QPainterPath, QPalette, QPen, QBrush, QColor,
    QKeySequence
)
from AnyQt.QtWidgets import (
    QLabel, QDoubleSpinBox, QGroupBox, QHBoxLayout, QAction,
    QGraphicsPathItem, QGraphicsRectItem, QGraphicsItem, QFormLayout,
    QApplication, QButtonGroup, QRadioButton, QCheckBox, QStackedWidget
)

from Orange.data import Table
from Orange.widgets import widget, gui, settings
from Orange.widgets.utils.concurrent import ThreadExecutor, FutureWatcher

from orangecontrib.resolwe.utils import ResolweHelper, ResolweTask


#: Filter type
Cells, Genes = 0, 1

#: Filter quality control measure (apply to Cell/Genes type only)
DetectionCount = 0  # number of genes/features with non-zero expression level
TotalCounts = 1  # total counts by cell/gene


#: Filter descriptions for various roles in UI
#: (short name, name, description)
FilterInfo = {
    Cells: ("Cells", "Cell Filter",
            "Filter cells (rows) by total counts (library size) or number "
            "of expressed genes."),
    Genes: ("Genes", "Gene Filter",
            "Filter genes (columns) by total counts mapped to a gene or "
            "number of cells in which the gene is expressed in.")
}

# Quality control measure descriptions for UI
MeasureInfo = {
    TotalCounts: ("Total counts",
                  "Sum of all counts across cell/gene"),
    DetectionCount: ("Detection count",
                     "Number of cells/genes with non-zero expression")
}


class ScatterPlotItem(pg.ScatterPlotItem):
    def paint(self, painter, *args):
        if self.opts["antialias"]:
            painter.setRenderHint(QPainter.Antialiasing, True)
        if self.opts["pxMode"]:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        super().paint(painter, *args)


class OWResolweFilter(widget.OWWidget):
    name = "Resolwe Filter"
    icon = 'icons/OWResolweFilter.svg'
    description = "Filter cells/genes"
    priority = 40

    class Inputs:
        data = widget.Input("Data", resolwe.Data)

    class Outputs:
        data = widget.Output("Data", resolwe.Data)

    class Warning(widget.OWWidget.Warning):
        invalid_range = widget.Msg(
            "Negative values in input data.\n"
            "This filter only makes sense for non-negative measurements "
            "where 0 indicates a lack (of) and/or a neutral reading."
        )
        sampling_in_effect = widget.Msg(
            "Too many data points to display.\n"
            "Sampling {} of {} data points."
        )

    #: Filter mode.
    #: Filter out rows/columns.
    Cells, Genes = Cells, Genes

    settings_version = 1

    #: The selected filter mode
    selected_filter_type = settings.Setting(Cells)  # type: int

    #: Selected filter statistics / QC measure indexed by filter_type
    selected_filter_metric = settings.Setting(TotalCounts)  # type: int

    #: Augment the violin plot with a dot plot (strip plot) of the (non-zero)
    #: measurement counts in Cells/Genes mode or data matrix values in Data
    #: mode.
    display_dotplot = settings.Setting(True)  # type: bool

    #: Is min/max range selection enable
    limit_lower_enabled = settings.Setting(True)  # type: bool
    limit_upper_enabled = settings.Setting(True)  # type: bool

    #: The lower and upper selection limit for each filter type
    thresholds = settings.Setting({
        (Cells, DetectionCount): (0, 2 ** 31 - 1),
        (Cells, TotalCounts): (0, 2 ** 31 - 1),
        (Genes, DetectionCount): (0, 2 ** 31 - 1),
        (Genes, TotalCounts): (0, 2 ** 31 - 1)
    })  # type: Dict[Tuple[int, int], Tuple[float, float]]

    auto_commit = settings.Setting(False)   # type: bool

    def __init__(self):
        super().__init__()
        self.data_table_object = None               # type: Optional[resolwe.Data]
        self._counts = None                         # type: Optional[np.ndarray]

        self._counts_data_obj = None                # type: Optional[resolwe.Data]
        self._counts_slug = 'data-filter-counts'    # type: str

        self._selection_data_obj = None             # type: Optional[resolwe.Data]
        self._selection_slug = 'data-table-filter'  # type: str

        # threading
        self._task = None                           # type: Optional[ResolweTask]
        self._executor = ThreadExecutor()

        self.res = ResolweHelper()

        box = gui.widgetBox(self.controlArea, "Info")
        self._info = QLabel(box)
        self._info.setWordWrap(True)
        self._info.setText("No data in input\n")

        box.layout().addWidget(self._info)

        box = gui.widgetBox(self.controlArea, "Filter Type", spacing=-1)
        rbg = QButtonGroup(box, exclusive=True)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        for id_ in [Cells, Genes]:
            name, _, tip = FilterInfo[id_]
            b = QRadioButton(
                name, toolTip=tip, checked=id_ == self.selected_filter_type
            )
            rbg.addButton(b, id_)
            layout.addWidget(b, stretch=10, alignment=Qt.AlignCenter)
        box.layout().addLayout(layout)

        rbg.buttonClicked[int].connect(self.set_filter_type)

        self.filter_metric_cb = gui.comboBox(
            box, self, "selected_filter_metric", callback=self._update_metric
        )
        for id_ in [DetectionCount, TotalCounts]:
            text, ttip = MeasureInfo[id_]
            self.filter_metric_cb.addItem(text)
            idx = self.filter_metric_cb.count() - 1
            self.filter_metric_cb.setItemData(idx, ttip, Qt.ToolTipRole)
        self.filter_metric_cb.setCurrentIndex(self.selected_filter_metric)

        form = QFormLayout(
            labelAlignment=Qt.AlignLeft,
            formAlignment=Qt.AlignLeft,
            fieldGrowthPolicy=QFormLayout.AllNonFixedFieldsGrow
        )
        self._filter_box = box = gui.widgetBox(
            self.controlArea, "Filter", orientation=form
        )  # type: QGroupBox

        self.threshold_stacks = (
            QStackedWidget(enabled=self.limit_lower_enabled),
            QStackedWidget(enabled=self.limit_upper_enabled),
        )
        finfo = np.finfo(np.float64)
        for filter_ in [Cells, Genes]:
            if filter_ in {Cells, Genes}:
                minimum = 0.0
                ndecimals = 1
                metric = self.selected_filter_metric
            else:
                minimum = finfo.min
                ndecimals = 3
                metric = -1
            spinlower = QDoubleSpinBox(
                self, minimum=minimum, maximum=finfo.max, decimals=ndecimals,
                keyboardTracking=False,
            )
            spinupper = QDoubleSpinBox(
                self, minimum=minimum, maximum=finfo.max, decimals=ndecimals,
                keyboardTracking=False,
            )

            lower, upper = self.thresholds.get((filter_, metric), (0, 0))

            spinlower.setValue(lower)
            spinupper.setValue(upper)

            self.threshold_stacks[0].addWidget(spinlower)
            self.threshold_stacks[1].addWidget(spinupper)

            spinlower.valueChanged.connect(self._limitchanged)
            spinupper.valueChanged.connect(self._limitchanged)

        self.threshold_stacks[0].setCurrentIndex(self.selected_filter_type)
        self.threshold_stacks[1].setCurrentIndex(self.selected_filter_type)

        self.limit_lower_enabled_cb = cb = QCheckBox(
            "Min", checked=self.limit_lower_enabled
        )
        cb.toggled.connect(self.set_lower_limit_enabled)
        cb.setAttribute(Qt.WA_LayoutUsesWidgetRect, True)
        form.addRow(cb, self.threshold_stacks[0])

        self.limit_upper_enabled_cb = cb = QCheckBox(
            "Max", checked=self.limit_upper_enabled
        )
        cb.toggled.connect(self.set_upper_limit_enabled)
        cb.setAttribute(Qt.WA_LayoutUsesWidgetRect, True)
        form.addRow(cb, self.threshold_stacks[1])

        box = gui.widgetBox(self.controlArea, "View")
        self._showpoints = gui.checkBox(
            box, self, "display_dotplot", "Show data points",
            callback=self._update_dotplot
        )

        self.controlArea.layout().addStretch(10)

        gui.auto_commit(self.controlArea, self, "auto_commit", "Commit")

        self._view = pg.GraphicsView()
        self._view.enableMouse(False)
        self._view.setAntialiasing(True)
        self._plot = plot = ViolinPlot()
        self._plot.setDataPointsVisible(self.display_dotplot)
        self._plot.setSelectionMode(
            (ViolinPlot.Low if self.limit_lower_enabled else 0) |
            (ViolinPlot.High if self.limit_upper_enabled else 0)
        )
        self._plot.selectionEdited.connect(self._limitchanged_plot)
        self._view.setCentralWidget(self._plot)
        self._plot.setTitle(FilterInfo[self.selected_filter_metric][1])

        bottom = self._plot.getAxis("bottom")  # type: pg.AxisItem
        bottom.hide()
        plot.setMouseEnabled(False, False)
        plot.hideButtons()
        self.mainArea.layout().addWidget(self._view)

        self.addAction(
            QAction("Select All", self, shortcut=QKeySequence.SelectAll, triggered=self._select_all)
        )

    def cancel(self):
        """Cancel the current task (if any)."""
        if self._task is not None:
            self._task.cancel()
            assert self._task.future.done()
            # disconnect the `_task_finished` slot
            self._task.watcher.done.disconnect(self.task_finished)
            self._task = None

    def run_task(self, slug, func):
        if self._task is not None:
            self.cancel()
        assert self._task is None

        self.progressBarInit()

        self._task = ResolweTask(slug)
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
            if self._task.slug == self._counts_slug:
                self._counts_data_obj = future_result
                self._setup_plot(future_result)

            elif self._task.slug == self._selection_slug:
                self._selection_data_obj = future_result
                self.Outputs.data.send(self._selection_data_obj)
                self._update_info()
                return self._selection_data_obj
        finally:
            self.progressBarFinished()
            self._task = None

    @Inputs.data
    def set_data(self, data):
        # type: (Optional[resolwe.Data]) -> None
        self.clear()
        self.data_table_object = data
        if data is not None:
            # self.res.get_object(id=data.id)
            self._setup(data, self.filter_type())

    def commit(self):
        if self._counts_data_obj:
            inputs = {'data_table': self.data_table_object,
                      'counts': self._counts_data_obj,
                      'axis': self._counts_data_obj.input['axis']}

            if self.limit_upper_enabled:
                inputs['upper_limit'] = self.limit_upper
            if self.limit_lower_enabled:
                inputs['lower_limit'] = self.limit_lower

            func = partial(self.res.run_process,
                           self._selection_slug,
                           **inputs)

            self.run_task(self._selection_slug, func)

        self.Outputs.data.send(None)
            
    def _setup(self, data, filter_type):
        self.clear()
        axis = 1 if filter_type == Cells else 0

        func = partial(self.res.run_process,
                       self._counts_slug,
                       data_table=data,
                       axis=axis,
                       measure=self.selected_filter_metric)

        # move filter process in thread
        self.run_task(self._counts_slug, func)

    def _setup_plot(self, data_object):
        # type: (resolwe.Data) -> None

        filter_data = self.res.get_json(data_object, 'counts_json', 'counts')
        axis_on_input = data_object.input['axis']
        measure = self.selected_filter_metric

        if axis_on_input == Cells:
            title = "Cell Filter"
            if measure == TotalCounts:
                axis_label = "Total counts (library size)"
            else:
                axis_label = "Number of expressed genes"
        else:
            title = "Gene Filter"
            if measure == TotalCounts:
                axis_label = "Total counts"
            else:
                # TODO: Too long
                axis_label = "Number of cells a gene is expressed in"

        span = -1.0  # data span
        x = np.asarray(filter_data)
        if x.size:
            span = np.ptp(x)

        self._counts = x
        self.Warning.sampling_in_effect.clear()

        spinlow = self.threshold_stacks[0].widget(axis_on_input)
        spinhigh = self.threshold_stacks[1].widget(axis_on_input)
        if measure == TotalCounts:
            if span > 0:
                ndecimals = max(4 - int(np.floor(np.log10(span))), 1)
            else:
                ndecimals = 1
        else:
            ndecimals = 1

        spinlow.setDecimals(ndecimals)
        spinhigh.setDecimals(ndecimals)

        if x.size:
            xmin, xmax = np.min(x), np.max(x)
            self.limit_lower = np.clip(self.limit_lower, xmin, xmax)
            self.limit_upper = np.clip(self.limit_upper, xmin, xmax)

        if x.size > 0:
            # TODO: Need correction for lower bounded distribution (counts)
            # Use reflection around 0, but gaussian_kde does not provide
            # sufficient flexibility w.r.t bandwidth selection.
            self._plot.setData(x, 1000)
            self._plot.setBoundary(self.limit_lower, self.limit_upper)

        ax = self._plot.getAxis("left")  # type: pg.AxisItem
        ax.setLabel(axis_label)
        self._plot.setTitle(title)
        self._update_info()

    def sizeHint(self):
        sh = super().sizeHint()  # type: QSize
        return sh.expandedTo(QSize(800, 600))

    def set_filter_type(self, type_):
        if self.selected_filter_type != type_:
            assert type_ in (Cells, Genes), str(type_)
            self.selected_filter_type = type_
            self.threshold_stacks[0].setCurrentIndex(type_)
            self.threshold_stacks[1].setCurrentIndex(type_)
            if self.data_table_object is not None:
                self._setup(self.data_table_object, type_)

    def filter_type(self):
        return self.selected_filter_type

    def _update_metric(self):
        if self.data_table_object is not None:
            self._setup(self.data_table_object, self.selected_filter_type, )

    def set_upper_limit_enabled(self, enabled):
        if enabled != self.limit_upper_enabled:
            self.limit_upper_enabled = enabled
            self.threshold_stacks[1].setEnabled(enabled)
            self.limit_upper_enabled_cb.setChecked(enabled)
            self._update_filter()

    def set_lower_limit_enabled(self, enabled):
        if enabled != self.limit_lower_enabled:
            self.limit_lower_enabled = enabled
            self.threshold_stacks[0].setEnabled(enabled)
            self.limit_lower_enabled_cb.setChecked(enabled)
            self._update_filter()

    def _update_filter(self):
        mode = 0
        if self.limit_lower_enabled:
            mode |= ViolinPlot.Low
        if self.limit_upper_enabled:
            mode |= ViolinPlot.High
        self._plot.setSelectionMode(mode)

    def _is_filter_enabled(self):
        return self.limit_lower_enabled or self.limit_upper_enabled

    def clear(self):
        self._plot.clear()
        self._selection_data_obj = None
        self._counts_data_obj = None
        self._counts = None
        self._update_info()
        self.Warning.clear()

    def _update_info(self):
        text = []
        if self.data_table_object:
            text.append('Input Data (object id): {}'.format(self.data_table_object.id))

        if self._selection_data_obj and self._counts_data_obj:
            num_selected = self._selection_data_obj.output.get('num_selected', None)

            axis = self._counts_data_obj.input.get('axis', None)
            if num_selected is not None and axis is not None:
                text.append('Output data ({instance}{s}): {num} '.format(
                    instance='gene' if axis == 0 else 'cell',
                    s='s' if num_selected > 0 else '',
                    num=num_selected
                ))

        self._info.setText('\n'.join(text))

    def _select_all(self):
        self.limit_lower = 0
        self.limit_upper = 2 ** 31 - 1
        self._limitchanged()

    def _update_dotplot(self):
        self._plot.setDataPointsVisible(self.display_dotplot)

    def current_filter_thresholds(self):
        if self.selected_filter_type in {Cells, Genes}:
            metric = self.selected_filter_metric
        else:
            metric = -1
        return self.thresholds[self.selected_filter_type, metric]

    def set_current_filter_thesholds(self, lower, upper):
        if self.selected_filter_type in {Cells, Genes}:
            metric = self.selected_filter_metric
        else:
            metric = -1
        self.thresholds[self.selected_filter_type, metric] = (lower, upper)

    @property
    def limit_lower(self):
        return self.current_filter_thresholds()[0]

    @limit_lower.setter
    def limit_lower(self, value):
        _, upper = self.current_filter_thresholds()
        self.set_current_filter_thesholds(value, upper)
        stacklower, _ = self.threshold_stacks
        sb = stacklower.widget(self.selected_filter_type)
        # prevent changes due to spin box rounding
        sb.setValue(value)

    @property
    def limit_upper(self):
        return self.current_filter_thresholds()[1]

    @limit_upper.setter
    def limit_upper(self, value):
        lower, _ = self.current_filter_thresholds()
        self.set_current_filter_thesholds(lower, value)
        _, stackupper = self.threshold_stacks
        sb = stackupper.widget(self.selected_filter_type)
        sb.setValue(value)

    @Slot()
    def _limitchanged(self):
        # Low/high limit changed via the spin boxes
        stacklow, stackhigh = self.threshold_stacks
        filter_ = self.selected_filter_type

        lower = stacklow.widget(filter_).value()
        upper = stackhigh.widget(filter_).value()
        self.set_current_filter_thesholds(lower, upper)

        if self._counts is not None and self._counts.size:
            xmin = np.min(self._counts)
            xmax = np.max(self._counts)
            self._plot.setBoundary(
                np.clip(lower, xmin, xmax),
                np.clip(upper, xmin, xmax)
            )

    def _limitchanged_plot(self):
        # Low/high limit changed via the plot
        if self._counts is not None:
            newlower, newupper = self._plot.boundary()
            filter_ = self.selected_filter_type
            lower, upper = self.current_filter_thresholds()
            stacklow, stackhigh = self.threshold_stacks
            spin_lower = stacklow.widget(filter_)
            spin_upper = stackhigh.widget(filter_)
            # do rounding to match the spin box's precision
            if self.limit_lower_enabled:
                newlower = round(newlower, spin_lower.decimals())
            else:
                newlower = lower

            if self.limit_upper_enabled:
                newupper = round(newupper, spin_upper.decimals())
            else:
                newupper = upper

            if self.limit_lower_enabled and newlower != lower:
                self.limit_lower = newlower
            if self.limit_upper_enabled and newupper != upper:
                self.limit_upper = newupper

            self._plot.setBoundary(newlower, newupper)

    def onDeleteWidget(self):
        self.data_table_object = None
        self.clear()
        self._plot.close()
        super().onDeleteWidget()

    @classmethod
    def migrate_settings(cls, settings, version):
        if (version is None or version < 2) and \
                ("limit_lower" in settings and "limit_upper" in settings):
            # v2 changed limit_lower, limit_upper to per filter limits stored
            # in a single dict
            lower = settings.pop("limit_lower")
            upper = settings.pop("limit_upper")
            settings["thresholds"] = {
                (Cells, TotalCounts): (lower, upper),
                (Cells, DetectionCount): (lower, upper),
                (Genes, TotalCounts): (lower, upper),
                (Genes, DetectionCount): (lower, upper),
            }
        if version == 2:
            thresholds = settings["thresholds"]
            c = thresholds.pop(Cells)
            g = thresholds.pop(Genes)
            thresholds = {
                (Cells, TotalCounts): c,
                (Cells, DetectionCount): c,
                (Genes, TotalCounts): g,
                (Genes, DetectionCount): g,
            }
            settings["thresholds"] = thresholds


@contextmanager
def block_signals(qobj):
    b = qobj.blockSignals(True)
    try:
        yield
    finally:
        qobj.blockSignals(b)


class ViolinPlot(pg.PlotItem):
    """
    A violin plot item with interactive data boundary selection.
    """
    #: Emitted when the selection boundary has changed
    selectionChanged = Signal()
    #: Emitted when the selection boundary has been edited by the user
    #: (by dragging the boundary lines)
    selectionEdited = Signal()

    #: Selection Flags
    NoSelection, Low, High = 0, 1, 2

    def __init__(self, *args, enableMenu=False, **kwargs):
        super().__init__(*args, enableMenu=enableMenu, **kwargs)
        self.__data = None
        #: min/max cutoff line positions
        self.__min = 0
        self.__max = 0
        self.__dataPointsVisible = True
        self.__selectionEnabled = True
        self.__selectionMode = ViolinPlot.High | ViolinPlot.Low
        self._plotitems = None

    def setData(self, data, nsamples, sample_range=None, color=Qt.magenta):
        assert np.all(np.isfinite(data))

        if data.size > 0:
            xmin, xmax = np.min(data), np.max(data)
        else:
            xmin = xmax = 0.0

        if sample_range is None:
            xrange = xmax - xmin
            sample_min = xmin - xrange * 0.025
            sample_max = xmax + xrange * 0.025
        else:
            sample_min, sample_max = sample_range

        sample = np.linspace(sample_min, sample_max, nsamples)
        if data.size < 2:
            est = np.full(sample.size, 1. / sample.size, )
        else:
            try:
                density = stats.gaussian_kde(data)
                est = density.evaluate(sample)
            except np.linalg.LinAlgError:
                est = np.zeros(sample.size)

        item = QGraphicsPathItem(violin_shape(sample, est))
        color = QColor(color)
        color.setAlphaF(0.5)
        item.setBrush(QBrush(color))
        pen = QPen(self.palette().color(QPalette.Shadow))
        pen.setCosmetic(True)
        item.setPen(pen)
        est_max = np.max(est)

        x = np.random.RandomState(0xD06F00D).uniform(
            -est_max, est_max, size=data.size
        )
        dots = ScatterPlotItem(
            x=x, y=data, size=3,
        )
        dots.setVisible(self.__dataPointsVisible)
        pen = QPen(self.palette().color(QPalette.Shadow), 1)
        hoverPen = QPen(self.palette().color(QPalette.Highlight), 1.5)
        cmax = SelectionLine(
            angle=0, pos=xmax, movable=True, bounds=(sample_min, sample_max),
            pen=pen, hoverPen=hoverPen
        )
        cmin = SelectionLine(
            angle=0, pos=xmin, movable=True, bounds=(sample_min, sample_max),
            pen=pen, hoverPen=hoverPen
        )
        cmax.setCursor(Qt.SizeVerCursor)
        cmin.setCursor(Qt.SizeVerCursor)

        selection_item = QGraphicsRectItem(
            QRectF(-est_max, xmin, est_max * 2, xmax - xmin)
        )
        selection_item.setPen(QPen(Qt.NoPen))
        selection_item.setBrush(QColor(0, 250, 0, 50))

        def update_selection_rect():
            mode = self.__selectionMode
            p = selection_item.parentItem()  # type: Optional[QGraphicsItem]
            while p is not None and not isinstance(p, pg.ViewBox):
                p = p.parentItem()
            if p is not None:
                viewbox = p  # type: pg.ViewBox
            else:
                viewbox = None
            rect = selection_item.rect()  # type: QRectF
            if mode & ViolinPlot.High:
                rect.setTop(cmax.value())
            elif viewbox is not None:
                rect.setTop(viewbox.viewRect().bottom())
            else:
                rect.setTop(cmax.maxRange[1])

            if mode & ViolinPlot.Low:
                rect.setBottom(cmin.value())
            elif viewbox is not None:
                rect.setBottom(viewbox.viewRect().top())
            else:
                rect.setBottom(cmin.maxRange[0])

            selection_item.setRect(rect.normalized())

        cmax.sigPositionChanged.connect(update_selection_rect)
        cmin.sigPositionChanged.connect(update_selection_rect)
        cmax.visibleChanged.connect(update_selection_rect)
        cmin.visibleChanged.connect(update_selection_rect)

        def setupper(line):
            ebound = self.__effectiveBoundary()
            elower, eupper = ebound
            mode = self.__selectionMode
            if not mode & ViolinPlot.High:
                return
            upper = line.value()
            lower = min(elower, upper)
            if lower != elower and mode & ViolinPlot.Low:
                self.__min = lower
                cmin.setValue(lower)

            if upper != eupper:
                self.__max = upper

            if ebound != self.__effectiveBoundary():
                self.selectionEdited.emit()
                self.selectionChanged.emit()

        def setlower(line):
            ebound = self.__effectiveBoundary()
            elower, eupper = ebound
            mode = self.__selectionMode
            if not mode & ViolinPlot.Low:
                return
            lower = line.value()
            upper = max(eupper, lower)
            if upper != eupper and mode & ViolinPlot.High:
                self.__max = upper
                cmax.setValue(upper)

            if lower != elower:
                self.__min = lower

            if ebound != self.__effectiveBoundary():
                self.selectionEdited.emit()
                self.selectionChanged.emit()

        cmax.sigPositionChanged.connect(setupper)
        cmin.sigPositionChanged.connect(setlower)
        selmode = self.__selectionMode
        cmax.setVisible(selmode & ViolinPlot.High)
        cmin.setVisible(selmode & ViolinPlot.Low)
        selection_item.setVisible(selmode)

        self.addItem(dots)
        self.addItem(item)
        self.addItem(cmax)
        self.addItem(cmin)
        self.addItem(selection_item)

        self.setRange(
            QRectF(-est_max, np.min(sample), est_max * 2, np.ptp(sample))
        )
        self._plotitems = SimpleNamespace(
            pointsitem=dots,
            densityitem=item,
            cmax=cmax,
            cmin=cmin,
            selection_item=selection_item
        )
        self.__min = xmin
        self.__max = xmax

    def setDataPointsVisible(self, visible):
        self.__dataPointsVisible = visible
        if self._plotitems is not None:
            self._plotitems.pointsitem.setVisible(visible)

    def setSelectionMode(self, mode):
        oldlower, oldupper = self.__effectiveBoundary()
        oldmode = self.__selectionMode
        mode = mode & 0b11
        if self.__selectionMode == mode:
            return

        self.__selectionMode = mode

        if self._plotitems is None:
            return

        cmin = self._plotitems.cmin
        cmax = self._plotitems.cmax
        selitem = self._plotitems.selection_item

        cmin.setVisible(mode & ViolinPlot.Low)
        cmax.setVisible(mode & ViolinPlot.High)
        selitem.setVisible(bool(mode))

        lower, upper = self.__effectiveBoundary()
        # The recorded values are not bounded by each other on gui interactions
        # when one is disabled. Rectify this now.
        if (oldmode ^ mode) & ViolinPlot.Low and mode & ViolinPlot.High:
            # Lower activated and High enabled
            lower = min(lower, upper)
        if (oldmode ^ mode) & ViolinPlot.High and mode & ViolinPlot.Low:
            # High activated and Low enabled
            upper = max(lower, upper)

        with block_signals(self):
            if lower != oldlower and mode & ViolinPlot.Low:
                cmin.setValue(lower)
            if upper != oldupper and mode & ViolinPlot.High:
                cmax.setValue(upper)

        self.selectionChanged.emit()

    def setBoundary(self, low, high):
        """
        Set the lower and upper selection boundary value.
        """
        changed = 0
        mode = self.__selectionMode
        if self.__min != low:
            self.__min = low
            changed |= mode & ViolinPlot.Low
        if self.__max != high:
            self.__max = high
            changed |= mode & ViolinPlot.High

        if changed:
            if self._plotitems:
                with block_signals(self):
                    if changed & ViolinPlot.Low:
                        self._plotitems.cmin.setValue(low)
                    if changed & ViolinPlot.High:
                        self._plotitems.cmax.setValue(high)

            self.selectionChanged.emit()

    def boundary(self):
        """
        Return the current lower and upper selection boundary values.
        """
        return self.__min, self.__max

    def __effectiveBoundary(self):
        # effective boundary, masked by selection mode
        low, high = -np.inf, np.inf
        if self.__selectionMode & ViolinPlot.Low:
            low = self.__min
        if self.__selectionMode & ViolinPlot.High:
            high = self.__max
        return low, high

    def clear(self):
        super().clear()
        self._plotitems = None

    def mouseDragEvent(self, event):
        mode = self.__selectionMode
        if mode != ViolinPlot.NoSelection and event.buttons() & Qt.LeftButton:
            start = event.buttonDownScenePos(Qt.LeftButton)  # type: QPointF
            pos = event.scenePos()  # type: QPointF
            cmin, cmax = self._plotitems.cmin, self._plotitems.cmax
            assert cmin.parentItem() is cmax.parentItem()
            pos = self.mapToItem(cmin.parentItem(), pos)
            start = self.mapToItem(cmin.parentItem(), start)
            if mode & ViolinPlot.Low and mode & ViolinPlot.High:
                lower, upper = min(pos.y(), start.y()), max(pos.y(), start.y())
                cmin.setValue(lower)
                cmax.setValue(upper)
            elif mode & ViolinPlot.Low:
                lower = pos.y()
                cmin.setValue(lower)
            elif mode & ViolinPlot.High:
                upper = pos.y()
                cmax.setValue(upper)
            event.accept()


def violin_shape(x, p):
    # type: (Sequence[float], Sequence[float]) -> QPainterPath
    points = [QPointF(pi, xi) for xi, pi in zip(x, p)]
    points += [QPointF(-pi, xi) for xi, pi in reversed(list(zip(x, p)))]
    poly = QPolygonF(points)
    path = QPainterPath()
    path.addPolygon(poly)
    return path


class SelectionLine(pg.InfiniteLine):
    def paint(self, painter, option, widget=None):
        brect = self.boundingRect()
        c = brect.center()
        line = QLineF(brect.left(), c.y(), brect.right(), c.y())
        t = painter.transform()
        line = t.map(line)
        painter.save()
        painter.resetTransform()
        painter.setPen(self.currentPen)
        painter.drawLine(line)
        painter.restore()


def main(argv=None):  # pragma: no cover
    app = QApplication(list(argv or sys.argv))
    argv = app.arguments()
    w = OWResolweFilter()
    if len(argv) > 1:
        filename = argv[1]
    else:
        filename = "brown-selected"  # bad example
    data = Table(filename)
    w.set_data(data)
    w.show()
    w.raise_()
    app.exec()
    w.saveSettings()
    w.onDeleteWidget()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
