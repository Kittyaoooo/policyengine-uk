import functools
from typing import List, Tuple, Union, Dict
from microdf.generic import MicroDataFrame, MicroSeries
from openfisca_core.populations import Population
import pandas as pd
import openfisca_uk
from openfisca_uk.entities import *
from openfisca_core.simulation_builder import SimulationBuilder
from openfisca_core.model_api import *
import numpy as np
import microdf as mdf
from tqdm import trange
import warnings

np.random.seed(0)


class Microsimulation:
    def __init__(
        self, *reforms: Tuple[Reform], year: int = 2020, dataset=None
    ):
        self.dataset = dataset
        self.year = year
        self.reforms = (dataset.input_reform, *reforms)
        self.load_dataset(dataset, year)
        self.entity_weights = dict(
            person=self.calc("person_weight", weighted=False),
            benunit=self.calc("benunit_weight", weighted=False),
            household=self.calc("household_weight", weighted=False),
        )
        self.bonus_sims = {}

    def map_to(
        self, arr: np.array, entity: str, target_entity: str, how: str = None
    ):
        entity_pop = self.simulation.populations[entity]
        target_pop = self.simulation.populations[target_entity]
        if entity == "person" and target_entity in ("benunit", "household"):
            if how and how not in (
                "sum",
                "any",
                "min",
                "max",
                "all",
                "value_from_first_person",
            ):
                raise ValueError("Not a valid function.")
            return target_pop.__getattribute__(how or "sum")(arr)
        elif entity in ("benunit", "household") and target_entity == "person":
            if not how:
                return entity_pop.project(arr)
            if how == "mean":
                return entity_pop.project(arr / entity_pop.nb_persons())
        elif entity == target_entity:
            return arr
        else:
            return self.map_to(
                self.map_to(arr, entity, "person", how="mean"),
                "person",
                target_entity,
                how="sum",
            )

    def calc(
        self,
        var: str,
        period: Union[str, int] = None,
        weighted: bool = True,
        map_to: str = None,
        how: str = None,
        dp: int = 2,
    ) -> MicroSeries:
        if period is None:
            period = self.year
        try:
            var_metadata = self.simulation.tax_benefit_system.variables[var]
            arr = self.simulation.calculate(var, period)
        except Exception as e:
            try:
                arr = self.simulation.calculate_add(var, period)
                if var_metadata.value_type == bool:
                    arr = arr >= 52
            except:
                try:
                    arr = self.simulation.calculate_divide(var, period)
                except:
                    raise e
        if var_metadata.value_type == float:
            arr = arr.round(2)
        if var_metadata.value_type == Enum:
            arr = arr.decode_to_str()
        if not weighted:
            return arr
        else:
            entity = var_metadata.entity.key
            if map_to:
                arr = self.map_to(arr, entity, map_to, how=how)
                entity = map_to
            return mdf.MicroSeries(arr, weights=self.entity_weights[entity])

    def df(
        self, vars: List[str], period: Union[str, int] = None, map_to=None
    ) -> MicroDataFrame:
        df = pd.DataFrame()
        entity = (
            map_to
            or self.simulation.tax_benefit_system.variables[vars[0]].entity.key
        )
        for var in vars:
            df[var] = self.calc(var, period=period, map_to=entity)
        df = MicroDataFrame(df, weights=self.entity_weights[entity])
        return df

    def apply_reforms(self, reforms: list) -> None:
        """Applies a list of reforms to the tax-benefit system.

        Args:
            reforms (list): A list of reforms. Each reform can also be a list of reforms.
        """
        for reform in reforms:
            if isinstance(reform, tuple) or isinstance(reform, list):
                self.apply_reforms(reform)
            else:
                self.system = reform(self.system)

    def load_dataset(self, dataset, year: int) -> None:
        data = dataset.load(year)
        year = str(year)
        self.system = openfisca_uk.CountryTaxBenefitSystem()
        self.apply_reforms(self.reforms)
        builder = SimulationBuilder()
        builder.create_entities(self.system)
        self.relations = {
            "person": np.array(data["P_person_id"][year]),
            "benunit": np.array(data["B_benunit_id"][year]),
            "household": np.array(data["H_household_id"][year]),
            "person-benunit": np.array(data["P_benunit_id"][year]),
            "person-household": np.array(data["P_household_id"][year]),
        }
        builder.declare_person_entity(
            "person", np.array(data["P_person_id"][year])
        )
        benunits = builder.declare_entity(
            "benunit", np.array(data["B_benunit_id"][year])
        )
        households = builder.declare_entity(
            "household", np.array(data["H_household_id"][year])
        )
        person_roles = np.array(np.array(data["P_role"][year]))
        builder.join_with_persons(
            benunits, np.array(data["P_benunit_id"][year]), person_roles
        )  # define person-benunit memberships
        builder.join_with_persons(
            households, np.array(data["P_household_id"][year]), person_roles
        )  # define person-household memberships
        model = builder.build(self.system)
        skipped = []
        for variable in data.keys():
            for period in data[variable].keys():
                try:
                    model.set_input(
                        variable, period, np.array(data[variable][period])
                    )
                except:
                    skipped += [variable]
        if skipped:
            warnings.warn(
                f"Incomplete initialisation: skipped {len(skipped)} variables:"
            )
        self.simulation = model

    def deriv(
        self,
        target="tax",
        wrt="employment_income",
        delta=100,
        percent=False,
        group_limit=2,
    ) -> MicroSeries:
        """Calculates effective marginal tax rates over a population.

        Args:
            targets (str, optional): The name of the variable to measure the derivative of. Defaults to "household_net_income".
            wrt (str, optional): The name of the independent variable. Defaults to "employment_income".

        Returns:
            np.array: [description]
        """
        system = self.simulation.tax_benefit_system
        target_entity = system.variables[target].entity.key
        wrt_entity = system.variables[wrt].entity.key
        if target_entity == wrt_entity:
            # calculating a derivative with both source and target in the same entity
            config = (wrt, delta, percent, "same-entity")
            if config not in self.bonus_sims:
                existing_var_class = system.variables[wrt].__class__

                altered_variable = type(wrt, (existing_var_class,), {})
                if not percent:
                    altered_variable.formula = (
                        lambda *args: existing_var_class.formula(*args) + delta
                    )
                else:
                    altered_variable.formula = (
                        lambda *args: existing_var_class.formula(*args)
                        * (1.0 + delta / 100)
                    )

                class bonus_ref(Reform):
                    def apply(self):
                        self.update_variable(altered_variable)

                self.bonus_sims[config] = Microsimulation(
                    self.reforms[1:] + (bonus_ref,),
                    mode=self.mode,
                    year=self.year,
                    input_year=self.input_year,
                )
            bonus_sim = self.bonus_sims[config]
            bonus_increase = bonus_sim.calc(wrt).astype(float) - self.calc(
                wrt
            ).astype(float)
            target_increase = bonus_sim.calc(target).astype(float) - self.calc(
                target
            ).astype(float)

            gradient = target_increase / bonus_increase

            return gradient
        elif (
            target_entity in ("benunit", "household")
            and wrt_entity == "person"
        ):
            # calculate the derivative for a group variable wrt a source variable, independent of other members in the group
            adult = self.calc("is_adult")
            index_in_group = (
                self.calc("person_id")
                .groupby(self.calc(f"{target_entity}_id", map_to="person"))
                .cumcount()
            )
            max_group_size = min(max(index_in_group[adult]) + 1, group_limit)

            derivative = np.empty((len(adult))) * np.nan

            for i in trange(
                max_group_size, desc="Calculating independent derivatives"
            ):
                config = (wrt, delta, percent, "group-entity", i)
                if config not in self.bonus_sims:
                    existing_var_class = system.variables[wrt].__class__

                    altered_variable = type(wrt, (existing_var_class,), {})
                    if not percent:
                        altered_variable.formula = (
                            lambda person, *args: existing_var_class.formula(
                                person, *args
                            )
                            + delta * (index_in_group == i) * adult
                        )
                    else:
                        delta /= 100
                        altered_variable.formula = (
                            lambda *args: existing_var_class.formula(*args)
                            * (1.0 + delta * (index_in_group == i) * adult)
                        )

                    class bonus_ref(Reform):
                        def apply(self):
                            self.update_variable(altered_variable)

                    self.bonus_sims[config] = Microsimulation(
                        self.reforms[1:] + (bonus_ref,),
                        mode=self.mode,
                        year=self.year,
                        input_year=self.input_year,
                    )
                bonus_sim = self.bonus_sims[config]
                bonus_increase = bonus_sim.calc(wrt).astype(float) - self.calc(
                    wrt
                ).astype(float)
                target_increase = bonus_sim.calc(
                    target, map_to="person"
                ).astype(float) - self.calc(target, map_to="person").astype(
                    float
                )
                result = target_increase / bonus_increase
                derivative[bonus_increase > 0] = result[bonus_increase > 0]

            return MicroSeries(
                derivative, weights=self.entity_weights["person"]
            )
        else:
            raise ValueError(
                "Unable to compute derivative - target variable must be from a group of or the same as the source variable"
            )

    def deriv_df(
        self, *targets, wrt="employment_income", delta=100, percent=False
    ) -> MicroDataFrame:
        wrt_entity = self.simulation.tax_benefit_system.variables[
            wrt
        ].entity.key
        df = MicroDataFrame(weights=self.entity_weights[wrt_entity])
        for target in targets:
            df[target] = self.deriv(
                target, wrt=wrt, delta=delta, percent=percent
            )
        return df
