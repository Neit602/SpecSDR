def open_window(qtbot, tmp_path, *extra):
    from pyrx.app import parse_args
    from pyrx.config.settings import Settings, set_config_dir
    from pyrx.ui.main_window import MainWindow
    set_config_dir(str(tmp_path))
    w = MainWindow(Settings().load(), parse_args(["--no-audio", *extra]))
    qtbot.addWidget(w)
    w.show()
    return w
