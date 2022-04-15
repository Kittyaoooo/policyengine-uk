from openfisca_uk.model_api import *


class pension_credit_reported(Variable):
    value_type = float
    entity = Person
    label = "Reported amount of Pension Credit"
    definition_period = YEAR
    unit = "currency-GBP"


class would_claim_PC(Variable):
    value_type = bool
    entity = BenUnit
    label = "Would claim Pension Credit"
    documentation = (
        "Whether this family would claim Pension Credit if eligible"
    )
    definition_period = YEAR

    def formula(benunit, period, parameters):
        reported_pc = aggr(benunit, period, ["pension_credit_reported"]) > 0
        takeup_rate = parameters(period).benefit.pension_credit.takeup
        imputed_takeup = random(benunit) < takeup_rate
        return (
            True
            | benunit("claims_all_entitled_benefits", period)
            | imputed_takeup
            | reported_pc
        )


class pension_credit_eligible(Variable):
    value_type = bool
    entity = BenUnit
    label = "Eligible for Pension Credit"
    definition_period = YEAR

    def formula(benunit, period):
        person_is_SP_age = benunit.members("is_SP_age", period)
        all_SP_age = benunit.all(person_is_SP_age)
        any_SP_age = benunit.any(person_is_SP_age)
        claiming_HB = benunit("housing_benefit", period) > 0
        return all_SP_age | (any_SP_age & claiming_HB)


class pension_credit_MG(Variable):
    value_type = float
    entity = BenUnit
    label = "Pension Credit (Minimum Guarantee) amount per week"
    definition_period = YEAR
    unit = "currency-GBP"

    def formula(benunit, period, parameters):
        pc = parameters(period).benefit.pension_credit
        mg = pc.minimum_guarantee
        relation_type = benunit("relation_type", period)
        personal_allowance = mg[relation_type] * WEEKS_IN_YEAR
        premiums = add(
            benunit, period, ["severe_disability_premium", "carer_premium"]
        )
        num_children = benunit("num_children", period)
        additional_child_bonus = pc.child * num_children
        applicable_amount = (
            personal_allowance + premiums + additional_child_bonus
        )
        return (
            applicable_amount
            * benunit("pension_credit_eligible", period)
            * benunit("would_claim_PC", period)
        )


class guarantee_credit_applicable_income(Variable):
    value_type = float
    entity = BenUnit
    label = "Applicable income for Pension Credit"
    definition_period = YEAR
    unit = "currency-GBP"
    reference = "https://www.legislation.gov.uk/uksi/2002/1792/regulation/15"

    def formula(benunit, period, parameters):
        INCOME_COMPONENTS = [
            "state_pension",
            "pension_income",
            "maintenance_income",
            "employment_income",
            "self_employment_income",
            "property_income",
            "savings_interest_income",
            "dividend_income",
            "basic_income",
        ]
        bi = parameters(period).contrib.ubi_center.basic_income
        income = aggr(benunit, period, INCOME_COMPONENTS)
        if not bi.interactions.include_in_means_tests:
            # Basic income is already in personal benefits, deduct if needed
            income -= add(benunit, period, ["basic_income"])
        tax = aggr(benunit, period, ["tax"])
        BENEFIT_COMPONENTS = [
            "child_benefit",
            "child_tax_credit",
            "working_tax_credit",
            "housing_benefit",
        ]
        benefits = add(benunit, period, BENEFIT_COMPONENTS)
        return max_(income + benefits - tax, 0)


class pension_credit_GC(Variable):
    value_type = float
    entity = BenUnit
    label = "Pension Credit (Guarantee Credit) amount"
    definition_period = YEAR
    unit = "currency-GBP"

    def formula(benunit, period, parameters):
        income = benunit("guarantee_credit_applicable_income", period)
        amount = max_(0, benunit("pension_credit_MG", period) - income)
        return benunit("pension_credit_eligible", period) * amount


class savings_credit_applicable_income(Variable):
    value_type = float
    entity = BenUnit
    label = "Applicable income for Savings Credit"
    definition_period = YEAR
    unit = "currency-GBP"
    reference = "https://www.gov.uk/government/publications/pension-credit-technical-guidance/a-detailed-guide-to-pension-credit-for-advisers-and-others#working-out-income-for-savings-credit"

    def formula(benunit, period, parameters):
        GC_income = benunit("guarantee_credit_applicable_income", period)
        EXEMPTED_PERSONAL_BENEFITS = [
            "incapacity_benefit",
            "JSA_contrib",
            "ESA_contrib",
            "sda",
            "maintenance_income",
        ]
        exempted_personal_benefits = aggr(
            benunit, period, EXEMPTED_PERSONAL_BENEFITS
        )
        EXEMPTED_FAMILY_BENEFITS = ["working_tax_credit"]
        exempted_family_benefits = add(
            benunit,
            period,
            EXEMPTED_FAMILY_BENEFITS,
        )
        return max_(
            GC_income - exempted_personal_benefits - exempted_family_benefits,
            0,
        )


class pension_credit_SC(Variable):
    value_type = float
    entity = BenUnit
    label = "Pension Credit (Savings Credit) amount per week"
    definition_period = YEAR
    unit = "currency-GBP"
    reference = "https://www.gov.uk/government/publications/pension-credit-technical-guidance/a-detailed-guide-to-pension-credit-for-advisers-and-others#legislation-60-return"

    def formula(benunit, period, parameters):
        SC = parameters(period).benefit.pension_credit.savings_credit
        income = benunit("savings_credit_applicable_income", period)
        appropriate_amount = benunit("pension_credit_MG", period)
        threshold = (
            SC.income_threshold[benunit("relation_type", period)]
            * WEEKS_IN_YEAR
        )
        maximum_amount = (1 - SC.withdrawal_rate) * max_(
            appropriate_amount - threshold, 0
        )
        amount_A = min_(
            (1 - SC.withdrawal_rate) * max_(income - threshold, 0),
            maximum_amount,
        )
        amount_B = max_(income - appropriate_amount, 0) * SC.withdrawal_rate
        amount = max_(amount_A - amount_B, 0)
        return amount * benunit("pension_credit_eligible", period)


class pension_credit(Variable):
    value_type = float
    entity = BenUnit
    label = "Pension Credit"
    definition_period = YEAR
    unit = "currency-GBP"

    def formula(benunit, period, parameters):
        COMPONENTS = ["pension_credit_GC", "pension_credit_SC"]
        return add(benunit, period, COMPONENTS) * benunit(
            "would_claim_PC", period
        )


class baseline_pension_credit(Variable):
    label = "Pension Credit (baseline)"
    entity = BenUnit
    definition_period = YEAR
    value_type = float
    unit = "currency-GBP"


class baseline_has_pension_credit(Variable):
    label = "Receives Pension Credit (baseline)"
    entity = BenUnit
    definition_period = YEAR
    value_type = bool
    default_value = True

    formula = baseline_is_nonzero(pension_credit)
