# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2015-2020 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Base class for a QtWebKit/QtWebEngine web inspector."""

import base64
import binascii
import typing
import enum

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import pyqtSignal, pyqtSlot, QObject, QEvent
from PyQt5.QtGui import QCloseEvent

from qutebrowser.browser import eventfilter
from qutebrowser.config import configfiles
from qutebrowser.utils import log, usertypes
from qutebrowser.keyinput import modeman
from qutebrowser.misc import miscwidgets, objects


def create(*, splitter: 'miscwidgets.InspectorSplitter',
           win_id: int,
           parent: QWidget = None) -> 'AbstractWebInspector':
    """Get a WebKitInspector/WebEngineInspector.

    Args:
        splitter: InspectorSplitter where the inspector can be placed.
        win_id: The window ID this inspector is associated with.
        parent: The Qt parent to set.
    """
    # Importing modules here so we don't depend on QtWebEngine without the
    # argument and to avoid circular imports.
    if objects.backend == usertypes.Backend.QtWebEngine:
        from qutebrowser.browser.webengine import webengineinspector
        if webengineinspector.supports_new():
            return webengineinspector.WebEngineInspector(
                splitter, win_id, parent)
        else:
            return webengineinspector.LegacyWebEngineInspector(
                splitter, win_id, parent)
    else:
        from qutebrowser.browser.webkit import webkitinspector
        return webkitinspector.WebKitInspector(splitter, win_id, parent)


class Position(enum.Enum):

    """Where the inspector is shown."""

    right = 1
    left = 2
    top = 3
    bottom = 4
    window = 5


class Error(Exception):

    """Raised when the inspector could not be initialized."""


class _EventFilter(QObject):

    """Event filter to enter insert mode when inspector was clicked.

    We need to use this with a ChildEventFilter (rather than just overriding
    mousePressEvent) for two reasons:

    - For QtWebEngine, we need to listen for mouse events on its focusProxy(),
      which can change when another page loads (which might be possible with an
      inspector as well?)

    - For QtWebKit, we need to listen for mouse events on the QWebView used by
      the QWebInspector.
    """

    def __init__(self, win_id: int, parent: QObject) -> None:
        super().__init__(parent)
        self._win_id = win_id

    def eventFilter(self, _obj: QObject, event: QEvent) -> bool:
        """Enter insert mode if the inspector is clicked."""
        if event.type() == QEvent.MouseButtonPress:
            modeman.enter(self._win_id, usertypes.KeyMode.insert,
                          reason='Inspector clicked')
        return False


class AbstractWebInspector(QWidget):

    """Base class for QtWebKit/QtWebEngine inspectors.

    Attributes:
        _position: position of the inspector (right/left/top/bottom/window)
        _splitter: InspectorSplitter where the inspector can be placed.

    Signals:
        recreate: Emitted when the inspector should be recreated.
    """

    recreate = pyqtSignal()

    def __init__(self, splitter: 'miscwidgets.InspectorSplitter',
                 win_id: int,
                 parent: QWidget = None) -> None:
        super().__init__(parent)
        self._widget = typing.cast(QWidget, None)
        self._layout = miscwidgets.WrapperLayout(self)
        self._splitter = splitter
        self._position = None  # type: typing.Optional[Position]
        self._event_filter = _EventFilter(win_id, parent=self)
        self._child_event_filter = eventfilter.ChildEventFilter(
            eventfilter=self._event_filter,
            win_id=win_id,
            parent=self)

    def _set_widget(self, widget: QWidget) -> None:
        self._widget = widget
        self._widget.setWindowTitle("Web Inspector")
        self._widget.installEventFilter(self._child_event_filter)
        self._layout.wrap(self, self._widget)

    def _load_position(self) -> Position:
        """Get the last position the inspector was in."""
        pos = configfiles.state['inspector'].get('position', 'right')
        return Position[pos]

    def _save_position(self, position: Position) -> None:
        """Save the last position the inspector was in."""
        configfiles.state['inspector']['position'] = position.name

    def _needs_recreate(self) -> bool:
        """Whether the inspector needs recreation when detaching to a window.

        This is done due to an unknown QtWebEngine bug which sometimes prevents
        inspector windows from showing up.

        Needs to be overridden by subclasses.
        """
        return False

    def set_position(self, position: typing.Optional[Position]) -> None:
        """Set the position of the inspector.

        If the position is None, the last known position is used.
        """
        if position is None:
            position = self._load_position()
        else:
            self._save_position(position)

        if position == self._position:
            self.toggle()
            return

        if (position == Position.window and
                self._position is not None and
                self._needs_recreate()):
            # Detaching to window
            self.recreate.emit()
            self.shutdown()
            return
        elif position == Position.window:
            self.setParent(None)  # type: ignore[call-overload]
            self._load_state_geometry()
        else:
            self._splitter.set_inspector(self, position)

        self._position = position

        self._widget.show()
        self.show()

    def toggle(self) -> None:
        """Toggle visibility of the inspector."""
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def _load_state_geometry(self) -> None:
        """Load the geometry from the state file."""
        try:
            data = configfiles.state['inspector']['window']
            geom = base64.b64decode(data, validate=True)
        except KeyError:
            # First start
            pass
        except binascii.Error:
            log.misc.exception("Error while reading geometry")
        else:
            log.init.debug("Loading geometry from {!r}".format(geom))
            ok = self._widget.restoreGeometry(geom)
            if not ok:
                log.init.warning("Error while loading geometry.")

    def closeEvent(self, _e: QCloseEvent) -> None:
        """Save the geometry when closed."""
        data = self._widget.saveGeometry().data()
        geom = base64.b64encode(data).decode('ASCII')
        configfiles.state['inspector']['window'] = geom

    def inspect(self, page: QWidget) -> None:
        """Inspect the given QWeb(Engine)Page."""
        raise NotImplementedError

    @pyqtSlot()
    def shutdown(self) -> None:
        """Clean up the inspector."""
        self.close()
        self.deleteLater()
