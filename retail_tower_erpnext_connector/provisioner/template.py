# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Company template v1, as versioned code (RT-1352 OD-2, OD-4). Accounting structure only: no tax
templates, no regional compliance app, no e-invoice setup, so the result reports
``fiscalReadiness: not_assessed``.

A template id and version are frozen into the request and into the site marker. A new version is a
new entry here; an existing site is never changed silently. This module imports no frappe: the
site-side get-or-create lives in :mod:`.site_ops`, which reads these values.
"""

from __future__ import annotations

from dataclasses import dataclass

FISCAL_READINESS = "not_assessed"
# The one internal Frappe API the template relies on is ``setup_complete``; it is pinned to the
# validated Frappe/ERPNext versions and covered by the bench-lane test (RT-1352 section 6).


@dataclass(frozen=True)
class TemplateSpec:
	template_id: str
	version: int
	country: str  # ERPNext Country name
	country_code: str
	currency: str
	timezone: str
	chart_of_accounts: str
	abbr: str
	cash_mode_of_payment: str
	walk_in_customer: str
	valuation_method: str
	perpetual_inventory: bool
	fiscal_year: str  # "calendar": 1 January to 31 December of the year of creation

	@property
	def key(self) -> str:
		return f"{self.template_id}@{self.version}"


TEMPLATE_EGYPT_RETAIL_V1 = TemplateSpec(
	template_id="egypt-retail",
	version=1,
	country="Egypt",
	country_code="EG",
	currency="EGP",
	timezone="Africa/Cairo",
	chart_of_accounts="Standard",  # v16 has no Egypt-localized chart (PV0, RT-1355)
	abbr="RT",  # constant, so generated account and warehouse names match on every site
	cash_mode_of_payment="Cash",
	walk_in_customer="Retail Walk-in",
	valuation_method="FIFO",
	perpetual_inventory=True,
	fiscal_year="calendar",
)

TEMPLATES: dict[tuple[str, int], TemplateSpec] = {
	(TEMPLATE_EGYPT_RETAIL_V1.template_id, TEMPLATE_EGYPT_RETAIL_V1.version): TEMPLATE_EGYPT_RETAIL_V1,
}


def spec_for(template_id: str, version: int) -> TemplateSpec | None:
	return TEMPLATES.get((template_id, version))


def locale_matches(spec: TemplateSpec, country: str, currency: str, timezone: str) -> bool:
	return (country, currency, timezone) == (spec.country_code, spec.currency, spec.timezone)
