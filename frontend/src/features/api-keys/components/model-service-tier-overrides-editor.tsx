import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SERVICE_TIERS, type ServiceTierType } from "@/features/api-keys/schemas";

export type ModelServiceTierOverridesEditorProps = {
  value: Record<string, ServiceTierType>;
  onChange: (value: Record<string, ServiceTierType>) => void;
  disabled?: boolean;
};

export function ModelServiceTierOverridesEditor({
  value,
  onChange,
  disabled = false,
}: ModelServiceTierOverridesEditorProps) {
  const { t } = useTranslation();
  const [draftModel, setDraftModel] = useState("");
  const [draftTier, setDraftTier] = useState<ServiceTierType>("priority");

  const addOverride = () => {
    const model = draftModel.trim().toLowerCase();
    if (!model) {
      return;
    }
    onChange({ ...value, [model]: draftTier });
    setDraftModel("");
  };

  const removeOverride = (model: string) => {
    const next = { ...value };
    delete next[model];
    onChange(next);
  };

  return (
    <div className="space-y-2">
      {Object.entries(value).map(([model, tier]) => (
        <div key={model} className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="min-w-0 flex-1 truncate rounded-md border bg-muted/20 px-2 py-1.5 text-xs">
            {model}
          </div>
          <Select
            value={tier}
            onValueChange={(nextTier) => onChange({ ...value, [model]: nextTier as ServiceTierType })}
          >
            <SelectTrigger
              className="h-8 w-full text-xs sm:w-32"
              disabled={disabled}
              aria-label={t("apiKeys.modelTierOverrides.tierAria", { model })}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              {SERVICE_TIERS.map((tierOption) => (
                <SelectItem key={tierOption} value={tierOption}>
                  {t(`common.serviceTier.${tierOption}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-8 text-xs sm:w-20"
            disabled={disabled}
            onClick={() => removeOverride(model)}
          >
            {t("apiKeys.modelTierOverrides.remove")}
          </Button>
        </div>
      ))}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input
          value={draftModel}
          disabled={disabled}
          onChange={(event) => setDraftModel(event.target.value)}
          className="h-8 text-xs"
          aria-label={t("apiKeys.modelTierOverrides.modelAria")}
          placeholder={t("apiKeys.modelTierOverrides.modelPlaceholder")}
        />
        <Select value={draftTier} onValueChange={(nextTier) => setDraftTier(nextTier as ServiceTierType)}>
          <SelectTrigger
            className="h-8 w-full text-xs sm:w-32"
            disabled={disabled}
            aria-label={t("apiKeys.modelTierOverrides.selectAria")}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent align="end">
            {SERVICE_TIERS.map((tierOption) => (
              <SelectItem key={tierOption} value={tierOption}>
                {t(`common.serviceTier.${tierOption}`)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-8 text-xs sm:w-24"
          disabled={disabled || !draftModel.trim()}
          onClick={addOverride}
        >
          {t("apiKeys.modelTierOverrides.add")}
        </Button>
      </div>
    </div>
  );
}
