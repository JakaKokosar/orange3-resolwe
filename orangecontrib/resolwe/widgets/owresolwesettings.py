""" OWResolweSettings """
import sys


from AnyQt.QtWidgets import QApplication
from Orange.widgets import widget, gui, settings


from orangecontrib.resolwe.utils import (
    DEFAULT_PASSWORD, DEFAULT_USERNAME, DEFAULT_URL,
    set_resolwe_password, set_resolwe_username, set_resolwe_url
)


class OWResolweSettings(widget.OWWidget):
    name = "Resolwe Settings"
    description = "Server Authentication"
    icon = "icons/OWResolweSettings.svg"
    priority = 10
    want_control_area = False

    server = settings.Setting(DEFAULT_URL)
    username = settings.Setting(DEFAULT_USERNAME)
    password = settings.Setting(DEFAULT_PASSWORD)

    def __init__(self):
        super().__init__()

        box = gui.widgetBox(self.controlArea, "Settings")

        gui.lineEdit(box, self,
                     'server',
                     label='Server',
                     callback=self.__update_enviroment_variables)

        gui.lineEdit(box, self,
                     'username',
                     label='Username',
                     callback=self.__update_enviroment_variables)

        gui.lineEdit(box, self,
                     'password',
                     label='Password',
                     callback=self.__update_enviroment_variables)

        self.mainArea.layout().addWidget(box)

    def __update_enviroment_variables(self):
        set_resolwe_url(self.server)
        set_resolwe_username(self.username)
        set_resolwe_password(self.password)


if __name__ == "__main__":

    def main(args=None):
        if args is None:
            args = sys.argv

        app = QApplication(list(args))
        w = OWResolweSettings()
        # w.resetSettings()
        w.show()
        w.raise_()
        rv = app.exec_()
        w.saveSettings()
        w.onDeleteWidget()
        return rv

    sys.exit(main())
