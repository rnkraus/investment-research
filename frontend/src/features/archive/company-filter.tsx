import { apiClient, type CompanySummary } from "@/lib/api/client";

import { ArchiveScopePicker } from "./archive-scope-picker";

export function CompanyFilter({
  company,
  disabled,
  onChange,
}: {
  readonly company: CompanySummary | null;
  readonly disabled: boolean;
  readonly onChange: (company: CompanySummary | null) => void;
}) {
  return (
    <ArchiveScopePicker
      selected={company}
      disabled={disabled}
      onChange={onChange}
      search={apiClient.listCompanies}
      itemKey={(item) => item.company_id}
      itemLabel={(item) =>
        item.ticker ? `${item.name} (${item.ticker})` : item.name
      }
      selectionLabel={() => "Company"}
      title="Company filter"
      inputLabel="Find a company by name or ticker"
      placeholder="Company name or ticker"
      resultsName="companies"
      clearLabel="Clear company filter"
    />
  );
}
