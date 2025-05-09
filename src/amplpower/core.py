import logging
from pathlib import Path

import numpy as np
import pandas as pd
from amplpy import AMPL
from matpowercaseframes import CaseFrames
from scipy.optimize import minimize

def compute(args):
    return max(args, key=len)


# TODO: Remove compute function at some point


def array2dict(array):
    """Convert a 2D numpy array to a dictionary."""
    return {(i, j): array[i, j] for i in range(array.shape[0]) for j in range(array.shape[1])}


class PowerSystem:
    """PowerSystem class for solving optimal power flow problems."""

    def __init__(self, case_file: str):
        """Initialize the power system with a MATPOWER case file."""
        print(f"=======Initializing the power system with case file: {case_file}")
        self.case_file = case_file
        self.max_angle = np.pi / 2
        self.min_angle = -np.pi / 2
        self.load_data()
        self.summary()
        self.compute_matrices()
        self.initialize()
        self.compute_initial_bigm_dc()
        self.compute_initial_bigm_ac()

    def load_data(self):
        """Load MATPOWER case data into DataFrames and convert to per unit."""
        try:
            case = CaseFrames(self.case_file)
            # Load data for each component
            self.baseMVA = case.baseMVA
            self.buses = case.bus
            self.buses.reset_index(drop=True, inplace=True)
            self.buses["BUS_I"] -= 1
            self.generators = case.gen
            self.generators.reset_index(drop=True, inplace=True)
            self.generators["GEN_BUS"] -= 1
            self.branches = case.branch
            self.branches.reset_index(drop=True, inplace=True)
            self.branches["F_BUS"] -= 1
            self.branches["T_BUS"] -= 1
            self.gencost = case.gencost
            self.gencost.reset_index(drop=True, inplace=True)
            self.nbus = len(self.buses)
            self.nlin = len(self.branches)
            self.ngen = len(self.generators)

            # Add default values for generator costs if not provided
            if "COST_2" not in self.gencost.columns:
                self.gencost["COST_2"] = 0

            # Minimum and maximum voltage limits
            self.max_voltage = self.buses["VMAX"].max()
            self.min_voltage = self.buses["VMIN"].min()
            self.buses["AMAX"] = self.max_angle
            self.buses["AMIN"] = self.min_angle

            # Convert to per unit
            self.buses["PD"] /= self.baseMVA
            self.buses["QD"] /= self.baseMVA
            self.buses["GS"] /= self.baseMVA
            self.buses["BS"] /= self.baseMVA
            self.generators["PG"] /= self.baseMVA
            self.generators["QG"] /= self.baseMVA
            self.generators["PMAX"] /= self.baseMVA
            self.generators["PMIN"] /= self.baseMVA
            self.generators["QMAX"] /= self.baseMVA
            self.generators["QMIN"] /= self.baseMVA
            self.branches["RATE_A"] /= self.baseMVA
            self.branches["RATE_B"] /= self.baseMVA
            self.branches["RATE_C"] /= self.baseMVA

            # Set default branch limit if not provided
            self.default_branch_limit = np.sqrt(self.buses["PD"].sum() ** 2 + self.buses["QD"].sum() ** 2)
            for line_index in range(self.nlin):
                if self.branches.loc[line_index, "RATE_A"] == 0:
                    self.branches.loc[line_index, "RATE_A"] = self.default_branch_limit

        except Exception as e:
            logging.error(f"Error loading data from {self.case_file}: {e}")
            raise

    def compute_matrices(self):
        """Calculate the admittance matrices (yff, ytf, yft, ytt) for the network."""
        # Initizalize matrices
        self.yff = np.zeros(self.nlin, dtype=complex)
        self.ytf = np.zeros(self.nlin, dtype=complex)
        self.yft = np.zeros(self.nlin, dtype=complex)
        self.ytt = np.zeros(self.nlin, dtype=complex)
        self.cf = np.zeros((self.nlin, self.nbus))  # Connection for F_BUS
        self.ct = np.zeros((self.nlin, self.nbus))  # Connection for T_BUS
        self.cg = np.zeros((self.ngen, self.nbus))  # Connection for generators
        # Compute admittance matrices
        for line_index in range(self.nlin):
            branch = self.branches.iloc[line_index]  # Access branch data
            r = branch["BR_R"]
            x = branch["BR_X"]
            b = branch["BR_B"]
            tau = branch["TAP"] if branch["TAP"] != 0 else 1  # Handle TAP=0 case
            theta = branch["SHIFT"]

            # Calculate Y series and shunt admittance
            ys = 1 / (r + 1j * x)

            # Store the admittance components
            self.yff[line_index] = (ys + 1j * 0.5 * b) / (tau**2)
            self.yft[line_index] = -ys / (tau * np.exp(-1j * theta))
            self.ytf[line_index] = -ys / (tau * np.exp(1j * theta))
            self.ytt[line_index] = ys + 1j * 0.5 * b

            # Update bus connection matrices
            f_bus, t_bus = int(branch["F_BUS"]), int(branch["T_BUS"])  # Ensure indices are integers
            self.cf[line_index, f_bus] = 1
            self.ct[line_index, t_bus] = 1

        # Compute additional matrices
        self.yf = np.dot(np.diag(self.yff), self.cf) + np.dot(np.diag(self.yft), self.ct)
        self.yt = np.dot(np.diag(self.ytf), self.cf) + np.dot(np.diag(self.ytt), self.ct)
        self.ysh = self.buses["GS"].values + 1j * self.buses["BS"].values
        self.yb = np.dot(np.transpose(self.cf), self.yf) + np.dot(np.transpose(self.ct), self.yt) + np.diag(self.ysh)

        # Include admittance values in the branch DataFrame
        self.branches["GFF"] = np.real(self.yff)
        self.branches["BFF"] = np.imag(self.yff)
        self.branches["GFT"] = np.real(self.yft)
        self.branches["BFT"] = np.imag(self.yft)
        self.branches["GTF"] = np.real(self.ytf)
        self.branches["BTF"] = np.imag(self.ytf)
        self.branches["GTT"] = np.real(self.ytt)
        self.branches["BTT"] = np.imag(self.ytt)

        # Compute generator connection matrix
        for g in range(self.ngen):
            bus = int(self.generators.iloc[g]["GEN_BUS"])  # Ensure index is an integer
            self.cg[g, bus] = 1

    def initialize(self, voltages=None, angles=None):
        """Initialize the voltage magnitudes, angles, flows, and generation levels."""
        if voltages is None:
            voltages = np.ones(self.nbus)
        if angles is None:
            angles = np.zeros(self.nbus)
        self.buses["VOL0"] = voltages
        self.buses["ANG0"] = angles
        self.buses["VOLR0"] = voltages * np.cos(angles)
        self.buses["VOLI0"] = voltages * np.sin(angles)

        # Compute flows
        v = voltages * np.exp(1j * angles)
        sf = (self.cf @ v) * np.conj(self.yf @ v)
        st = (self.ct @ v) * np.conj(self.yt @ v)
        self.branches["PF0"] = np.real(sf)
        self.branches["QF0"] = np.imag(sf)
        self.branches["PT0"] = np.real(st)
        self.branches["QT0"] = np.imag(st)

        # Compute generator outputs
        sd = self.buses["PD"].values + 1j * self.buses["QD"].values
        sb = v * np.conj(self.yb @ v)
        sg = sb + sd
        self.generators["PG0"] = np.dot(np.real(sg), self.cg.T)
        self.generators["QG0"] = np.dot(np.imag(sg), self.cg.T)

    def summary(self):
        """Print summary of the network."""
        print(f"Number of buses: {self.nbus}")
        print(f"Number of lines: {self.nlin}")
        print(f"Number of generators: {self.ngen}")
        print(f"baseMVA: {self.baseMVA}")
        print("\nBuses:")
        print(self.buses.head())
        print("\nGenerators:")
        print(self.generators.head())
        print("\nBranches:")
        print(self.branches.head())
        print("\nGenerator Costs:")
        print(self.gencost.head())

    def compute_initial_bigm_dc(self):
        """Compute Big-M values for DC the different lines and return them in a DataFrame."""
        print("=======Computing initial bigM values for DC power flow")
        self.branches["PFUPDC"] = (1 / self.branches["BR_X"]) * (self.cf @ self.buses["AMAX"] - self.ct @ self.buses["AMIN"])
        self.branches["PFLODC"] = (1 / self.branches["BR_X"]) * (self.cf @ self.buses["AMIN"] - self.ct @ self.buses["AMAX"])
        # print(self.branches[["PFUPDC", "PFLODC"]])

    def compute_initial_bigm_ac(self):
        """Compute Big-M values for AC the different lines and return them in a DataFrame."""
        print("=======Computing initial bigM values for AC power flow")
        self.branches["PFUPAC"] = np.zeros(self.nlin)
        self.branches["PFLOAC"] = np.zeros(self.nlin)
        self.branches["PTUPAC"] = np.zeros(self.nlin)
        self.branches["PTLOAC"] = np.zeros(self.nlin)
        self.branches["QFUPAC"] = np.zeros(self.nlin)
        self.branches["QFLOAC"] = np.zeros(self.nlin)
        self.branches["QTUPAC"] = np.zeros(self.nlin)
        self.branches["QTLOAC"] = np.zeros(self.nlin)
        self.branches["COSFTMAX"] = np.zeros(self.nlin)
        self.branches["COSFTMIN"] = np.zeros(self.nlin)
        self.branches["SINFTMAX"] = np.zeros(self.nlin)
        self.branches["SINFTMIN"] = np.zeros(self.nlin)
        for lin_index in range(self.nlin):  # Changed 'lin' to 'lin_index'
            f_bus = int(self.branches.loc[lin_index, "F_BUS"])
            t_bus = int(self.branches.loc[lin_index, "T_BUS"])
            amaxf = self.buses.loc[f_bus, "AMAX"]
            aminf = self.buses.loc[f_bus, "AMIN"]
            amaxt = self.buses.loc[t_bus, "AMAX"]
            amint = self.buses.loc[t_bus, "AMIN"]
            vmaxf = self.buses.loc[f_bus, "VMAX"]
            vminf = self.buses.loc[f_bus, "VMIN"]
            vmaxt = self.buses.loc[t_bus, "VMAX"]
            vmint = self.buses.loc[t_bus, "VMIN"]
            x0 = [(vmaxf + vminf) / 2, (vmaxt + vmint) / 2, (amaxf + aminf) / 2, (amaxt + amint) / 2]

            def pfac(x, lin_index=lin_index):
                return (
                    self.branches.loc[lin_index, "GFF"] * x[0] * x[0]
                    + self.branches.loc[lin_index, "GFT"] * x[0] * x[1] * np.cos(x[2] - x[3])
                    + self.branches.loc[lin_index, "BFT"] * x[0] * x[1] * np.sin(x[2] - x[3])
                )

            def ptac(x, lin_index=lin_index):
                return (
                    self.branches.loc[lin_index, "GTT"] * x[1] * x[1]
                    + self.branches.loc[lin_index, "GTF"] * x[0] * x[1] * np.cos(x[3] - x[2])
                    + self.branches.loc[lin_index, "BTF"] * x[0] * x[1] * np.sin(x[3] - x[2])
                )

            def qfac(x, lin_index=lin_index):
                return (
                    -self.branches.loc[lin_index, "BFF"] * x[0] * x[0]
                    - self.branches.loc[lin_index, "BFT"] * x[0] * x[1] * np.cos(x[2] - x[3])
                    + self.branches.loc[lin_index, "GFT"] * x[0] * x[1] * np.sin(x[2] - x[3])
                )

            def qtac(x, lin_index=lin_index):
                return (
                    -self.branches.loc[lin_index, "BTT"] * x[1] * x[1]
                    - self.branches.loc[lin_index, "BTF"] * x[0] * x[1] * np.cos(x[3] - x[2])
                    + self.branches.loc[lin_index, "GTF"] * x[0] * x[1] * np.sin(x[3] - x[2])
                )

            def cosft(x):
                return x[0] * x[1] * np.cos(x[2] - x[3])

            def sinft(x):
                return x[0] * x[1] * np.sin(x[2] - x[3])

            self.branches.loc[lin_index, "PFUPAC"] = (
                -1 * minimize(lambda x: -pfac(x), x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]).fun
            )
            self.branches.loc[lin_index, "PFLOAC"] = minimize(
                pfac, x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]
            ).fun
            self.branches.loc[lin_index, "PTUPAC"] = (
                -1 * minimize(lambda x: -ptac(x), x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]).fun
            )
            self.branches.loc[lin_index, "PTLOAC"] = minimize(
                ptac, x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]
            ).fun
            self.branches.loc[lin_index, "QFUPAC"] = (
                -1 * minimize(lambda x: -qfac(x), x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]).fun
            )
            self.branches.loc[lin_index, "QFLOAC"] = minimize(
                qfac, x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]
            ).fun
            self.branches.loc[lin_index, "QTUPAC"] = (
                -1 * minimize(lambda x: -qtac(x), x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]).fun
            )
            self.branches.loc[lin_index, "QTLOAC"] = minimize(
                qtac, x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]
            ).fun
            self.branches.loc[lin_index, "COSFTMAX"] = (
                -1 * minimize(lambda x: -cosft(x), x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]).fun
            )
            self.branches.loc[lin_index, "COSFTMIN"] = minimize(
                cosft, x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]
            ).fun
            self.branches.loc[lin_index, "SINFTMAX"] = (
                -1 * minimize(lambda x: -sinft(x), x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]).fun
            )
            self.branches.loc[lin_index, "SINFTMIN"] = minimize(
                sinft, x0, bounds=[(vminf, vmaxf), (vmint, vmaxt), (aminf, amaxf), (amint, amaxt)]
            ).fun
        # print(self.branches[["PFUPAC", "PFLOAC", "PTUPAC", "PTLOAC", "QFUPAC", "QFLOAC", "QTUPAC", "QTLOAC"]])

    def solve_opf(self, opf_type="dc", switching="off", connectivity="off", solver="gurobi", options="outlev=1 timelimit=3600"):
        """Solve the optimal power flow problem using AMPL.
        Parameters:
        opf_type (str): Type of optimal power flow ('dc', 'acrect', 'acjabr')
        switching (str): Switching strategy ('off', 'nl', 'bigm')
        connectivity (str): Connectivity for topology solutions ('off', 'on')
        solver (str): Solver to use ('gurobi', 'cplex', 'cbc')
        options (str): Options for the solver
        Returns:
        dict: Results of the optimal power flow problem
        """
        # set the status of the lines
        if isinstance(switching, np.ndarray):
            self.branches["BR_STATUS"] = switching
        elif switching == "off":
            self.branches["BR_STATUS"] = 1
        elif switching == "nl":
            self.branches["BR_STATUS"] = 2
        elif switching == "bigm":
            self.branches["BR_STATUS"] = 3

        print(
            f"=======Solving OPF ({opf_type}) with switching {switching} and connectivity {connectivity} with solver {solver} and options {options}"
        )
        ampl = AMPL()
        ampl.read(Path(__file__).parent / "opf.mod")

        ampl.set_data(self.buses, "N")
        ampl.set_data(self.generators, "G")
        ampl.set_data(self.branches, "L")
        ampl.set_data(self.gencost)
        ampl.param["CF"] = array2dict(self.cf)
        ampl.param["CT"] = array2dict(self.ct)
        ampl.param["CG"] = array2dict(self.cg)
        ampl.param["OPF_TYPE"] = opf_type
        ampl.param["CONNECTIVITY"] = connectivity
        ampl.param["BASEMVA"] = self.baseMVA
        ampl.param["MAXVOL"] = self.max_voltage
        ampl.param["MINVOL"] = self.min_voltage

        ampl.option["mp_options"] = options
        if solver != 'ipopt': # ipopt is not an MP solver but ASL
            ampl.option["ipopt_options"] = options
        else:
            ampl.option["mp_options"] = options
        ampl.solve(solver=solver)
        solver_status = ampl.solve_result

        if solver_status == "solved" or solver_status == "limit":
            # Get the generation results
            Pg = ampl.get_variable("Pg").get_values().to_pandas().values.flatten()
            Qg = ampl.get_variable("Qg").get_values().to_pandas().values.flatten()
            Pg_ls = np.minimum(Pg - self.generators["PMIN"].values, 0)
            Pg_us = np.maximum(Pg - self.generators["PMAX"].values, 0)
            Qg_ls = np.minimum(Qg - self.generators["QMIN"].values, 0)
            Qg_us = np.maximum(Qg - self.generators["QMAX"].values, 0)
            gen_df = pd.DataFrame(
                {"Pg": Pg, "Qg": Qg, "Pg_ls": Pg_ls, "Pg_us": Pg_us, "Qg_ls": Qg_ls, "Qg_us": Qg_us},
                index=ampl.get_variable("Pg").get_values().to_pandas().index,
            )

            # Get the line results
            switching = ampl.get_variable("status").get_values().to_pandas().values.flatten()
            Pf = ampl.get_variable("Pf").get_values().to_pandas().values.flatten()
            Pt = ampl.get_variable("Pt").get_values().to_pandas().values.flatten()
            Qf = ampl.get_variable("Qf").get_values().to_pandas().values.flatten()
            Qt = ampl.get_variable("Qt").get_values().to_pandas().values.flatten()
            Sf = Pf + 1j * Qf
            St = Pt + 1j * Qt
            Sf_us = np.maximum(abs(Sf) - self.branches["RATE_A"].values, 0)
            St_us = np.maximum(abs(St) - self.branches["RATE_A"].values, 0)
            line_df = pd.DataFrame(
                {
                    "switching": switching,
                    "Pf": Pf,
                    "Pt": Pt,
                    "Qf": Qf,
                    "Qt": Qt,
                    "Sf": abs(Sf),
                    "St": abs(St),
                    "Sf_us": Sf_us,
                    "St_us": St_us,
                },
                index=ampl.get_variable("status").get_values().to_pandas().index,
            )

            # Get the voltage results
            if opf_type == "acrect":
                volr = ampl.get_variable("Vr").get_values().to_pandas().values.flatten()
                voli = ampl.get_variable("Vi").get_values().to_pandas().values.flatten()
                Vm = np.sqrt(volr**2 + voli**2)
                Va = np.arctan2(voli, volr)
            elif opf_type == "acjabr":
                vol2 = ampl.get_variable("V2").get_values().to_pandas().values.flatten()
                Vm = np.sqrt(vol2)
                vfvtcosft = ampl.get_variable("cosft").get_values().to_pandas().values.flatten()
                vfvt = np.array([Vm[int(self.branches.loc[i, "F_BUS"])] * Vm[int(self.branches.loc[i, "T_BUS"])] for i in range(self.nlin)])
                cosft = np.maximum(-1, np.minimum(1, vfvtcosft / vfvt))
                # Compute angles for all buses
                Va = np.full(self.nbus, np.nan)  # Initialize angles with NaN
                Va[0] = 0  # Reference bus angle is 0
                # Iteratively compute angles
                visited = {0}  # Start with the reference bus
                while len(visited) < self.nbus:
                    for line_index in range(self.nlin):
                        f_bus = int(self.branches.loc[line_index, "F_BUS"])
                        t_bus = int(self.branches.loc[line_index, "T_BUS"])
                        if f_bus in visited and np.isnan(Va[t_bus]):
                            Va[t_bus] = Va[f_bus] + np.arccos(cosft[line_index])
                            visited.add(t_bus)
                        elif t_bus in visited and np.isnan(Va[f_bus]):
                            Va[f_bus] = Va[t_bus] - np.arccos(cosft[line_index])
                            visited.add(f_bus)
            else:
                Vm = ampl.get_variable("Vm").get_values().to_pandas().values.flatten()
                Va = ampl.get_variable("Va").get_values().to_pandas().values.flatten()
            Vm_ls = np.minimum(Vm - self.buses["VMIN"].values, 0)
            Vm_us = np.maximum(Vm - self.buses["VMAX"].values, 0)
            Va_ls = np.minimum(Va - self.buses["AMIN"].values, 0)
            Va_us = np.maximum(Va - self.buses["AMAX"].values, 0)
            # Computation of power injections
            Sd = self.buses["PD"].values + 1j * self.buses["QD"].values
            Sg = Pg + 1j * Qg
            Ssh = self.buses["GS"].values * Vm**2 - 1j * self.buses["BS"].values * Vm**2
            S_slack = Sg @ self.cg - Sd - Ssh - Sf @ self.cf - St @ self.ct
            P_slack = np.real(S_slack)
            Q_slack = np.imag(S_slack)
            bus_df = pd.DataFrame(
                {
                    "Vm": Vm,
                    "Va": Va,
                    "Vm_ls": Vm_ls,
                    "Vm_us": Vm_us,
                    "Va_ls": Va_ls,
                    "Va_us": Va_us,
                    "P_slack": P_slack,
                    "Q_slack": Q_slack,
                },
                index=ampl.get_variable("Vm").get_values().to_pandas().index,
            )

            return {
                "obj": ampl.get_objective("total_cost").value(),
                "time": ampl.get_value("_solve_time"),
                "gen": gen_df,
                "bus": bus_df,
                "lin": line_df,
                "status": "solved",
            }

        else:
            return {"obj": None, "time": None, "gen": None, "bus": None, "lin": None, "status": solver_status}
