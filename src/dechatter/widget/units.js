// Lengths, feeds and removal rates as the machine's units show them. Everything is stored in
// millimetres; on a machine in G20 it is shown in inches (4 decimals), with mm alongside
// where there is room, as the printed report does.

export const MM_PER_INCH = 25.4;
const MM3_PER_IN3 = MM_PER_INCH ** 3;

// Enough decimals that a hundred-thousandth of an inch survives the trip to millimetres.
export const tidy = (number) => Number(number.toFixed(5));

// "5.08 mm", or "0.2000 in" and with `mmToo` "0.2000 in (5.08 mm)". Millimetres drop
// trailing zeros: "5 mm".
export function length(mm, units, { mmToo = false, places = 2, inchPlaces = 4 } = {}) {
  const metric = `${Number(mm.toFixed(places))} mm`;
  if (units !== 'in') {
    return metric;
  }
  const inches = `${(mm / MM_PER_INCH).toFixed(inchPlaces)} in`;
  return mmToo ? `${inches} (${metric})` : inches;
}

// A chip load is a few hundredths of a millimetre: one more decimal each way.
export function chipLoad(mm, units, options = {}) {
  return length(mm, units, { ...options, places: 4, inchPlaces: 5 });
}

export function feed(mmPerMin, units, { mmToo = false } = {}) {
  const metric = `${Math.round(mmPerMin)} mm/min`;
  if (units !== 'in') {
    return metric;
  }
  const inches = `${(mmPerMin / MM_PER_INCH).toFixed(1)} in/min`;
  return mmToo ? `${inches} (${metric})` : inches;
}

export function rate(cm3PerMin, units, { mmToo = false } = {}) {
  const metric = `${cm3PerMin.toFixed(2)} cm³/min`;
  if (units !== 'in') {
    return metric;
  }
  const inches = `${((cm3PerMin * 1000) / MM3_PER_IN3).toFixed(3)} in³/min`;
  return mmToo ? `${inches} (${metric})` : inches;
}
