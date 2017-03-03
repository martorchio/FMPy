# noinspection PyPep8

import shutil
import zipfile
from tempfile import mkdtemp
from fmpy.modelDescription import read_model_description
from fmpy import fmi1, fmi2, FMIType
from fmpy.fmi1 import fmi1ValueReference, fmi1Real, fmi1Integer, fmi1Boolean
from fmpy.fmi2 import fmi2True, fmi2False
import numpy as np


class Recorder(object):

    def __init__(self, fmu, output=None):

        self.fmu = fmu

        md = fmu.modelDescription

        if md.fmiVersion == '1.0':
            self._getReal = fmu.fmi1GetReal
            self._getInteger = fmu.fmi1GetInteger
            self._getBoolean = fmu.fmi1GetBoolean
        else:
            self._getReal = fmu.fmi2GetReal
            self._getInteger = fmu.fmi2GetInteger
            self._getBoolean = fmu.fmi2GetBoolean

        self.cols = [('time', np.float64)]
        self.rows = []

        self.values = {'Real': [], 'Integer': [], 'Boolean': [], 'String': []}

        for sv in md.modelVariables:
            if (output is None and sv.causality == 'output') or (output is not None and sv.name in output):
                self.values['Integer' if sv.type == 'Enumeration' else sv.type].append((sv.name, sv.valueReference))

        if len(self.values['Real']) > 0:
            real_names, real_vrs = zip(*self.values['Real'])
            self.real_vrs = (fmi1ValueReference * len(real_vrs))(*real_vrs)
            self.real_values = (fmi1Real * len(real_vrs))()
            self.cols += zip(real_names, [np.float64] * len(real_names))
        else:
            self.real_vrs = []

        if len(self.values['Integer']) > 0:
            integer_names, integer_vrs = zip(*self.values['Integer'])
            self.integer_vrs = (fmi1ValueReference * len(integer_vrs))(*integer_vrs)
            self.integer_values = (fmi1Integer * len(integer_vrs))()
            self.cols += zip(integer_names, [np.int32] * len(integer_names))
        else:
            self.integer_vrs = []

        if len(self.values['Boolean']) > 0:
            boolean_names, boolean_vrs = zip(*self.values['Boolean'])
            self. boolean_vrs = (fmi1ValueReference * len(boolean_vrs))(*boolean_vrs)
            self.boolean_values = (fmi1Boolean * len(boolean_vrs))()
            self.cols += zip(boolean_names, [np.int32] * len(boolean_names))
        else:
            self.boolean_vrs = []

    def sample(self, time):

        row = [time]

        if len(self.real_vrs) > 0:
            status = self._getReal(self.fmu.component, self.real_vrs, len(self.real_vrs), self.real_values)
            row += list(self.real_values)

        if len(self.integer_vrs) > 0:
            status = self._getInteger(self.fmu.component, self.integer_vrs, len(self.integer_vrs), self.integer_values)
            row += list(self.integer_values)

        if len(self.boolean_vrs) > 0:
            status = self._getBoolean(self.fmu.component, self.boolean_vrs, len(self.boolean_vrs), self.boolean_values)
            row += list(self.boolean_values)

        self.rows.append(tuple(row))

    def result(self):
        return np.array(self.rows, dtype=np.dtype(self.cols))


def simulate(filename, start_time=None, stop_time=None, step_size=None, fmiType=None, start_values={}, output=None):

    modelDescription = read_model_description(filename)

    if fmiType is None:
        # determine the FMI type automatically
        fmiType = FMIType.CO_SIMULATION if modelDescription.coSimulation is not None else FMIType.MODEL_EXCHANGE

    defaultExperiment = modelDescription.defaultExperiment

    if start_time is None:
        if defaultExperiment is not None:
            start_time = defaultExperiment.startTime
        else:
            start_time = 0.0

    if stop_time is None:
        if defaultExperiment is not None:
            stop_time = defaultExperiment.stopTime
        else:
            stop_time = 1.0

    if step_size is None:
        total_time = stop_time - start_time
        step_size = 10 ** (np.round(np.log10(0.09)) - 3)

    unzipdir = mkdtemp()

    # expand the 8.3 paths on windows
    if sys.platform == 'win32':
        import win32file
        unzipdir = win32file.GetLongPathName(unzipdir)

    with zipfile.ZipFile(filename, 'r') as fmufile:
        fmufile.extractall(unzipdir)

    if modelDescription.fmiVersion == '1.0':
        if fmiType is FMIType.MODEL_EXCHANGE:
            return simulateME1(modelDescription, unzipdir, start_time, stop_time, step_size, output)
        else:
            return simulateCS1(modelDescription, unzipdir, start_time, stop_time, step_size, output)
    else:
        if fmiType is FMIType.MODEL_EXCHANGE:
            return simulateME2(modelDescription, unzipdir, start_time, stop_time, step_size, output)
        else:
            return simulateCS2(modelDescription, unzipdir, start_time, stop_time, step_size, output)

    # clean up
    shutil.rmtree(unzipdir)


def simulateME1(modelDescription, unzipdir, start_time, stop_time, step_size, output):

    fmu = fmi1.FMU1Model(modelDescription=modelDescription, unzipDirectory=unzipdir)
    fmu.instantiate()
    fmu.setTime(start_time)
    fmu.initialize()

    recorder = Recorder(fmu=fmu, output=output)

    prez  = np.zeros_like(fmu.z)

    time = start_time

    while time < stop_time:

        fmu.getContinuousStates()
        fmu.getDerivatives()

        tPre = time;
        time = min(time + step_size, stop_time);

        timeEvent = fmu.eventInfo.upcomingTimeEvent != fmi1.fmi1False and fmu.eventInfo.nextEventTime <= time;

        if timeEvent:
            time = fmu.eventInfo.nextEventTime

        dt = time - tPre

        fmu.setTime(time)

        # forward Euler
        fmu.x += dt * fmu.dx

        fmu.setContinuousStates()

        # check for step event, e.g.dynamic state selection
        stepEvent = fmu.completedIntegratorStep()

        # check for state event
        prez[:] = fmu.z
        fmu.getEventIndicators()
        stateEvent = np.any((prez * fmu.z) < 0)

        # handle events
        if timeEvent or stateEvent or stepEvent:
            fmu.eventUpdate()

        recorder.sample(time)

    fmu.terminate()
    fmu.freeInstance()

    return recorder.result()


def simulateCS1(modelDescription, unzipdir, start_time, stop_time, step_size, output):

    fmu = fmi1.FMU1Slave(modelDescription=modelDescription, unzipDirectory=unzipdir)
    fmu.instantiate("rectifier1")
    fmu.initialize()

    recorder = Recorder(fmu=fmu, output=output)

    time = start_time

    while time < stop_time:
        recorder.sample(time)
        status = fmu.doStep(currentCommunicationPoint=time, communicationStepSize=step_size)
        time += step_size

    fmu.terminate()
    fmu.freeInstance()

    return recorder.result()


def simulateME2(modelDescription, unzipdir, start_time, stop_time, step_size, output):

    fmu = fmi2.FMU2Model(modelDescription=modelDescription, unzipDirectory=unzipdir)
    fmu.instantiate()
    fmu.setupExperiment(tolerance=None, startTime=start_time)
    fmu.enterInitializationMode()
    fmu.exitInitializationMode()

    # event iteration
    fmu.eventInfo.newDiscreteStatesNeeded = fmi2True
    fmu.eventInfo.terminateSimulation = fmi2False

    while fmu.eventInfo.newDiscreteStatesNeeded == fmi2True and fmu.eventInfo.terminateSimulation == fmi2False:
        # update discrete states
        status = fmu.newDiscreteStates()

    fmu.enterContinuousTimeMode()

    recorder = Recorder(fmu=fmu, output=output)

    prez  = np.zeros_like(fmu.z)

    time = start_time

    recorder.sample(time)

    while time < stop_time:

        fmu.getContinuousStates()
        fmu.getDerivatives()

        tPre = time
        time = min(time + step_size, stop_time)

        timeEvent = fmu.eventInfo.nextEventTimeDefined != fmi2False and fmu.eventInfo.nextEventTime <= time

        if timeEvent:
            time = fmu.eventInfo.nextEventTime

        dt = time - tPre

        fmu.setTime(time)

        # forward Euler
        fmu.x += dt * fmu.dx

        fmu.setContinuousStates()

        # check for state event
        prez[:] = fmu.z
        fmu.getEventIndicators()
        stateEvent = np.any((prez * fmu.z) < 0)

        if stateEvent:
            print(time)

        # check for step event
        stepEvent, terminateSimulation = fmu.completedIntegratorStep()

        if timeEvent or stateEvent or stepEvent != fmi2False:

            # handle events
            fmu.enterEventMode()

            fmu.eventInfo.newDiscreteStatesNeeded = fmi2True
            fmu.eventInfo.terminateSimulation = fmi2False

            # update discrete states
            while fmu.eventInfo.newDiscreteStatesNeeded != fmi2False and fmu.eventInfo.terminateSimulation == fmi2False:
                fmu.newDiscreteStates()

            fmu.enterContinuousTimeMode()

        recorder.sample(time)

    fmu.terminate()
    fmu.freeInstance()

    return recorder.result()


def simulateCS2(modelDescription, unzipdir, start_time, stop_time, step_size, output):

    fmu = fmi2.FMU2Slave(modelDescription=modelDescription, unzipDirectory=unzipdir)
    fmu.instantiate()
    fmu.setupExperiment(tolerance=None, startTime=start_time)
    fmu.enterInitializationMode()
    fmu.exitInitializationMode()

    recorder = Recorder(fmu=fmu, output=output)

    time = start_time

    while time < stop_time:
        recorder.sample(time)
        fmu.doStep(currentCommunicationPoint=time, communicationStepSize=step_size)
        time += step_size

    fmu.terminate()
    fmu.freeInstance()

    return recorder.result()


if __name__ == '__main__':

    import sys
    import matplotlib.pyplot as plt

    if len(sys.argv) < 2:
        print("Usage:")
        print("> python fmpy.simulate <FMU>")
        exit(-1)

    filename = sys.argv[1]

    result = simulate(filename=filename, output=['h', 'v'])

    time = result['time']
    names = result.dtype.names[1:]

    if len(names) > 0:

        fig, ax = plt.subplots(len(names), sharex=True)

        fig.set_facecolor('white')

        for i, name in enumerate(names):
            ax[i].plot(time, result[name])
            ax[i].set_ylabel(name)
            ax[i].grid(True)
            ax[i].margins(y=0.1)

        plt.tight_layout()
        plt.show()
