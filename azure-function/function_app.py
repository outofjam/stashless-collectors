"""
Stashless collector as an Azure Function (Python, programming model v2):
one timer-triggered function that runs the collector on the schedule in
the STASHLESS_SCHEDULE app setting (an NCRONTAB expression, hourly by
default). The collector reads its configuration from the app settings,
which Functions exposes as environment variables, and takes any secret
named *_NAME from Key Vault through the Function app's managed identity.
A non-zero exit from the collector fails the invocation, so the
Function's own monitoring shows it.
"""

import logging

import azure.functions as func

import stashless_collector

app = func.FunctionApp()


@app.timer_trigger(schedule="%STASHLESS_SCHEDULE%", arg_name="timer", run_on_startup=False, use_monitor=True)
def report(timer: func.TimerRequest) -> None:
    code = stashless_collector.main([])
    logging.info("Stashless collector finished with exit code %d", code)

    if code != 0:
        raise RuntimeError("The Stashless collector exited with code %d; see the lines above for what it could not read or post." % code)
