# `parts-fact`

### Record datasheet-verified electrical facts — `fluxplace parts-fact MPN key=value ... --source FILE.pdf#pN [--datasheets D] [--force]`

Writes facts into the part's entry in `<datasheets>/datasheets.json` (`"facts": {key: {value, source}}`). The cited file must exist, the page is read, and a number that is not on that page is refused (`--force` overrides). A fact with no source is ignored by every check. The checks read them in `review` and in the parts stage (before placement): [elec](elec.md).

Keys: `vin_min` `vin_max` `supply_pins` (JSON list) `i_max_ma` (number or `{pin: mA}`) `i_out_max_a` `contact_a` `v_rated` `pin_rules`.

```
fluxplace parts-fact TPS2051CDBVR vin_min=4.5 vin_max=5.5 supply_pins='["IN"]' --source TPS2051CDBVR.pdf#p5
```
