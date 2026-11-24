# 3D tray reports

Compare a tray when it settles and just before it leaves. Rotate or zoom either 3D view to move both together.
The table lists which stones the SDK marked as removed, added or moved, with a confidence score for each change.

Open the notebook in Colab on a standard CPU runtime and choose **Runtime → Run all** to try the sample recording.
[Open in Colab](https://colab.research.google.com/github/eprojac/pdigger-factory-sdk-python/blob/main/notebooks/pDigger_Factory_3D_Tray_Reports.ipynb) · [View the notebook on GitHub](pDigger_Factory_3D_Tray_Reports.ipynb)

To use your own `.ndjson` or `.ndjson.gz` recording, set `SOURCE_MODE` to `upload`, `path` or `url`. For a path or
URL, enter it in `RECORDING`. Choose a `REPORT_NUMBER` from the overview table and rerun the comparison cell.
Set `EXPORT` to `True` in the last cell to save the 3D view as HTML and the full report as JSON.
