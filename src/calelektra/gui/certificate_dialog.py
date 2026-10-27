"""Calibration certificate of the cable of a test (``procedures/report.py``): reference, date and file."""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.gui import style
from calelektra.gui.test_common import SETTINGS, notify
from calelektra.procedures.record import TestRecord
from calelektra.procedures.report import (
    ReportError,
    certificate_problem,
    default_certificate_path,
    generate_certificate,
    prepare_certificate,
)


class CertificateDialog(QDialog):
    """Reference, date of the report and file of the certificate, with what is missing in the data of the test."""

    def __init__(self, library: Library, record: TestRecord, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.record = record
        self.saved: Path | None = None
        self.certificate = prepare_certificate(library, record)  # ReportError if the test cannot give one
        self.setWindowTitle("Certificado de calibración")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        context = self.certificate.context
        layout.addWidget(
            style.PageHeader(
                "Certificado de calibración",
                f"{context['eut']['instrumento']} · {context['eut']['referencia']} · resultado {context['resultado_final']}",
            )
        )
        form = QFormLayout()
        self.reference = QLineEdit(self.certificate.reference)
        self.reference.setToolTip("«Ref.» del encabezado y título del documento (por defecto: referencia del EUT_año_mes)")
        self.report_date = QDateEdit(QDate.currentDate())
        self.report_date.setCalendarPopup(True)
        self.report_date.setDisplayFormat("dd/MM/yyyy")
        self.path = QLineEdit(str(default_certificate_path(record, self.certificate.reference)))
        self._path_edited = False
        self.path.textEdited.connect(self._path_changed)
        browse = QPushButton("Examinar…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form.addRow("Referencia", self.reference)
        form.addRow("Fecha del informe", self.report_date)
        form.addRow("Fichero", row)
        rows = len(context["fwd"])
        form.addRow("Plantilla", QLabel(f"{self.certificate.template.name} · {rows} frecuencias por tabla (Fwd y Rev)"))
        layout.addLayout(form)
        self.reference.textChanged.connect(self._reference_changed)
        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        if self.certificate.warnings:
            items = "".join(f"<li>{html.escape(text)}</li>" for text in self.certificate.warnings)
            self.warnings.setText(f"<b>Avisos</b><ul style='margin-left:-20px'>{items}</ul>")
            self.warnings.setStyleSheet(f"color: {style.WARNING.name()};")
        else:
            self.warnings.setText("Todos los datos del certificado están en el ensayo y en la biblioteca.")
        layout.addWidget(self.warnings, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        generate = QPushButton("Generar")
        generate.setObjectName("primary")
        generate.setDefault(True)
        generate.clicked.connect(self.generate)
        buttons.addWidget(cancel)
        buttons.addWidget(generate)
        layout.addLayout(buttons)

    def _path_changed(self) -> None:
        self._path_edited = True

    def _reference_changed(self, text: str) -> None:
        if not self._path_edited:
            self.path.setText(str(default_certificate_path(self.record, text.strip() or self.certificate.reference)))

    def _browse(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "Certificado de calibración", self.path.text(), "Documento de Word (*.docx)")
        if filename:
            self.path.setText(filename if filename.lower().endswith(".docx") else f"{filename}.docx")
            self._path_edited = True

    def generate(self) -> Path | None:
        selected = self.report_date.date()
        try:
            path, _certificate = generate_certificate(
                self.library,
                self.record,
                Path(self.path.text().strip()),
                reference=self.reference.text(),
                report_date=date(selected.year(), selected.month(), selected.day()),
            )
        except (ReportError, OSError) as exc:
            QMessageBox.warning(self, "Certificado de calibración", str(exc))
            return None
        self.saved = path
        QSettings(*SETTINGS).setValue("carpeta_certificados", str(path.parent))
        self.accept()
        return path


def make_certificate(parent: QWidget, library: Library, record: TestRecord, path: str | Path | None = None) -> Path | None:
    """The certificate of the cable of a test: from the dialog or, with ``path``, directly with the defaults."""
    problem = certificate_problem(record)
    if problem:
        notify(parent, "Certificado de calibración", problem, warning=True)
        return None
    try:
        if path is not None:
            saved, certificate = generate_certificate(library, record, Path(path))
            warnings = certificate.warnings
        else:
            dialog = CertificateDialog(library, record, parent)
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.saved is None:
                return None
            saved, warnings = dialog.saved, dialog.certificate.warnings
    except ReportError as exc:
        notify(parent, "Certificado de calibración", str(exc), warning=True)
        return None
    text = f"Certificado guardado en {saved}."
    if warnings:
        text += "\n\nRevíselo antes de emitirlo:\n" + "\n".join(f"• {warning}" for warning in warnings)
    box = notify(parent, "Certificado de calibración", text, warning=bool(warnings))
    opener = box.addButton("Abrir", QMessageBox.ButtonRole.ActionRole)
    opener.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(saved))))
    return saved
