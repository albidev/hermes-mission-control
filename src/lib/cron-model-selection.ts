export type CronModelProviderOption = {
  slug: string;
  name: string;
  models: string[];
};

export type CronSelectOption = { value: string; label: string };

export function cronProviderOptions(
  providers: CronModelProviderOption[],
  currentProvider: string,
  inheritLabel: string,
): CronSelectOption[] {
  const options: CronSelectOption[] = [
    { value: '', label: inheritLabel },
    ...providers
      .filter((provider) => provider.models.length > 0 || provider.slug === currentProvider)
      .map((provider) => ({ value: provider.slug, label: `${provider.name} (${provider.slug})` })),
  ];
  if (currentProvider && !options.some((option) => option.value === currentProvider)) {
    options.push({ value: currentProvider, label: currentProvider });
  }
  return options;
}

export function cronModelOptions(
  providers: CronModelProviderOption[],
  provider: string,
  currentModel: string,
  inheritLabel: string,
): CronSelectOption[] {
  const models = providers.find((item) => item.slug === provider)?.models ?? [];
  const options: CronSelectOption[] = [
    { value: '', label: inheritLabel },
    ...models.map((model) => ({ value: model, label: model })),
  ];
  if (currentModel && !options.some((option) => option.value === currentModel)) {
    options.push({ value: currentModel, label: currentModel });
  }
  return options;
}

export function isCronModelPairValid(model: string, provider: string): boolean {
  return Boolean(model.trim()) === Boolean(provider.trim());
}

export function modelSelectionPayload(model: string, provider: string): { model: string | null; provider: string | null } {
  return {
    model: model.trim() || null,
    provider: provider.trim() || null,
  };
}
