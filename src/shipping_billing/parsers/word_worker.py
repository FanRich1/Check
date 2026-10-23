"""Isolated Word conversion worker; never attach to the user's running application."""

import sys
from pathlib import Path


def convert(input_path, output_path, pid_path, file_format=16):
    import pythoncom
    import win32com.client
    import win32process

    pythoncom.CoInitialize()
    app = doc = None
    old_security = old_links = None
    try:
        app = win32com.client.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        old_security = app.AutomationSecurity
        old_links = app.Options.UpdateLinksAtOpen
        app.AutomationSecurity = 3
        app.Options.UpdateLinksAtOpen = False
        doc = app.Documents.Add()
        pid = win32process.GetWindowThreadProcessId(doc.ActiveWindow.Hwnd)[1]
        Path(pid_path).write_text(str(pid), encoding="ascii")
        doc.Close(SaveChanges=0)
        doc = None
        doc = app.Documents.Open(
            FileName=str(Path(input_path).resolve()),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
            PasswordDocument="BILLING-NO-PASSWORD",
            WritePasswordDocument="BILLING-NO-PASSWORD",
        )
        doc.SaveAs2(
            FileName=str(Path(output_path).resolve()), FileFormat=int(file_format), AddToRecentFiles=False
        )
    finally:
        try:
            if doc is not None:
                doc.Close(SaveChanges=0)
        finally:
            if app is not None:
                if old_security is not None:
                    app.AutomationSecurity = old_security
                if old_links is not None:
                    app.Options.UpdateLinksAtOpen = old_links
                app.Quit(SaveChanges=0)
            pythoncom.CoUninitialize()


if __name__ == "__main__":
    try:
        convert(*sys.argv[1:])
    except Exception:
        print("WORD_CONVERSION_FAILED", file=sys.stderr)
        raise SystemExit(1)
