"""Runners that execute a workflow on a runtime other than Claude Code.

Claude Code is measured after the fact, by reading its transcript. Every other
runtime has to be measured from the inside: the runner drives the model, reads
`usage` off each real API response, and posts those records to the survey API.
Same survey, same fields, different runtime - which is the point, since a
benchmark that only works on one runtime cannot compare two.
"""
