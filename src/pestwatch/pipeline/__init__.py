"""The model pipeline: weather downloads, SEAS5, the LSTM and the pest risk models.

``nbrun`` and ``layout`` use only the standard library and are shared with the
Django side. ``steps``, ``seas5`` and ``stepper`` run in the separate model
environment (``/opt/pestmodels``) and are never imported by GeoNode.
"""
