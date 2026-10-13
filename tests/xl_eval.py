"""Evaluate the workbook's formulas without Excel (pycel), so tests can compare them with the Python model."""
import warnings

from openpyxl import load_workbook
from pycel import ExcelCompiler, excelwrapper


def _defined_names(self):
    # pycel 1.0b30 reads openpyxl's pre-3.1 defined-name list; openpyxl 3.1 keeps a dict.
    if self.workbook is not None and self._defined_names is None:
        self._defined_names = {
            str(n): [(alias, ws) for ws, alias in d.destinations if ws in self.workbook]
            for n, d in self.workbook.defined_names.items()}
    return self._defined_names


excelwrapper.ExcelOpxWrapper.defined_names = property(_defined_names)


class Workbook:
    def __init__(self, path):
        warnings.simplefilter("ignore")
        self.xl = ExcelCompiler(filename=str(path))
        self.names = {k: v.attr_text.replace("$", "") for k, v in load_workbook(path).defined_names.items()}

    def name(self, n):
        return self.xl.evaluate(self.names[n])

    def cell(self, ref):
        return self.xl.evaluate(ref)
