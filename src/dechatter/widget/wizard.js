// Setup wizard: the session's setup as five short forms. Values are kept in millimetres, as
// the setup file has them; on a machine in inches they are only shown and typed in inches.
// Each step shows what has to be answered about this machine, tool and stock; fields marked
// `advanced` have a sensible default or computed value and sit in a collapsed section.

const MM_PER_INCH = 25.4;
const LENGTH_UNITS = {
  length: { mm: 'mm', in: 'in' },
  lengths: { mm: 'mm', in: 'in' },
  feed: { mm: 'mm/min', in: 'in/min' },
};

export const STEPS = [
  {
    title: 'Machine',
    hint: 'The mill this tool runs in.',
    fields: [
      { at: 'machine.name', advanced: true, label: 'Name', type: 'text', optional: true },
      { at: 'machine.max_rpm', label: 'Highest spindle speed', type: 'int', unit: 'rpm' },
      { at: 'machine.min_rpm', label: 'Lowest usable spindle speed', type: 'int', unit: 'rpm' },
      { at: 'machine.spindle_power_w', label: 'Spindle motor power', type: 'number', unit: 'W' },
      { at: 'machine.weight_kg', label: 'Machine weight', type: 'number', unit: 'kg', note: 'Light machines start with a lighter chip load.' },
      {
        at: 'machine.spindle',
        label: 'Spindle speed is set by',
        type: 'choice',
        options: [['program', 'the program (M3 S)'], ['manual', 'me, by hand']],
        note: 'By hand, the program stops before each speed and tells you what to set.',
      },
    ],
  },
  {
    title: 'Tool',
    hint: 'One session per tool assembly: holder, cutter and stickout. Change any of them and the results no longer apply.',
    fields: [
      { at: 'tool.diameter_mm', label: 'Cutter diameter', type: 'length' },
      { at: 'tool.flutes', label: 'Flutes', type: 'int' },
      { at: 'tool.stickout_mm', label: 'Stickout from the holder', type: 'length' },
      { at: 'tool.holder', advanced: true, label: 'Holder', type: 'text', optional: true },
      { at: 'tool.material', label: 'Cutter material', type: 'choice', options: [['carbide', 'carbide'], ['hss', 'high-speed steel']] },
    ],
  },
  {
    title: 'Cut',
    hint: 'Each depth is tested on its own, stepping the spindle speed and the radial width.',
    fields: [
      { at: 'cut.material', label: 'Work material', type: 'choice', options: 'materials' },
      { at: 'cut.axial_depth_mm', label: 'Axial depth of cut', type: 'length', note: 'The deepest cut you want. It is tested first.' },
      { at: 'cut.other_depths_mm', label: 'Shallower depths to compare', type: 'lengths', optional: true, placeholder: 'none', note: 'Separated by commas. Tested afterwards, starting from the widths that were stable.' },
      { at: 'cut.direction', label: 'Direction', type: 'choice', options: [['conventional', 'conventional'], ['climb', 'climb']] },
      { at: 'cut.width_max_mm', advanced: true, label: 'Widest radial cut to try', type: 'length', optional: true, placeholder: 'half the diameter' },
      { at: 'cut.chip_load_mm', advanced: true, label: 'Chip load per tooth', type: 'length', optional: true, placeholder: 'from the material' },
      { at: 'cut.speed_min_rpm', advanced: true, label: 'Slowest speed to try', type: 'int', unit: 'rpm', optional: true, placeholder: 'a quarter of the fastest' },
      { at: 'cut.speed_max_rpm', advanced: true, label: 'Fastest speed to try', type: 'int', unit: 'rpm', optional: true, placeholder: 'machine or material limit' },
    ],
  },
  {
    title: 'Stock and program',
    hint: 'A block the test can cut up, clamped low and rigidly.',
    fields: [
      { at: 'stock.length_mm', label: 'Stock length (X)', type: 'length', note: 'Every pass cuts the full length.' },
      { at: 'stock.width_mm', label: 'Stock width to use (Y)', type: 'length', note: 'Material the test may consume from the front face.' },
      { at: 'program.coolant', advanced: true, label: 'Coolant', type: 'choice', options: [['none', 'none'], ['flood', 'flood'], ['mist', 'mist']], note: 'Coolant noise makes chatter harder to hear.' },
      { at: 'program.dwell_s', advanced: true, label: 'Pause at speed before each pass', type: 'number', unit: 's' },
      { at: 'program.clearance_mm', advanced: true, label: 'Clearance around the stock', type: 'length' },
      { at: 'program.safe_z_mm', label: 'Safe height (Z)', type: 'length' },
      { at: 'program.plunge_feed_mm_min', advanced: true, label: 'Plunge feed', type: 'feed' },
      { at: 'program.truing_width_mm', advanced: true, label: 'Truing pass width', type: 'length' },
    ],
  },
  {
    title: 'Planner',
    hint: 'How boldly the next cuts are chosen. The defaults suit most sessions.',
    fields: [
      { at: 'planner.round1_speeds', advanced: true, label: 'Speeds in round 1', type: 'int' },
      { at: 'planner.round1_width_fractions', advanced: true, label: 'Round 1 widths', type: 'list', note: 'Fractions of the widest radial cut, separated by commas.' },
      { at: 'planner.batch_size', advanced: true, label: 'Most cuts in a later round', type: 'int' },
      { at: 'planner.min_probability', advanced: true, label: 'Chance of being stable a cut needs', type: 'number', note: 'Assuming the cut before it passes. Between 0 and 1.' },
      { at: 'planner.probability_floor', advanced: true, label: 'The same, on measured cuts alone', type: 'number' },
      { at: 'planner.stop_improvement', advanced: true, label: 'Stop when a cut would add less than', type: 'number', note: 'As a fraction of the best removal rate so far.' },
    ],
  },
];

// Enough decimals that a hundred-thousandth of an inch survives the trip to millimetres.
const tidy = (number) => Number(number.toFixed(5));

function shown(field, value, units) {
  if (value === null || value === undefined) {
    return '';
  }
  if (field.type === 'lengths') {
    return value.map((length) => (units === 'in' ? tidy(length / MM_PER_INCH) : length)).join(', ');
  }
  if (field.type in LENGTH_UNITS && units === 'in') {
    return String(tidy(value / MM_PER_INCH));
  }
  if (field.type === 'list') {
    return value.join(', ');
  }
  return String(value);
}

function typed(field, text, units) {
  if (field.type === 'text' || field.type === 'choice') {
    return text.trim();
  }
  if (field.type === 'list') {
    return text.split(',').map((part) => Number(part.trim()));
  }
  if (field.type === 'lengths') {
    const lengths = text.trim() === '' ? [] : text.split(',').map((part) => Number(part.trim()));
    return lengths.map((length) => (units === 'in' ? tidy(length * MM_PER_INCH) : length));
  }
  if (text.trim() === '' || Number.isNaN(Number(text))) {
    return null;
  }
  const number = Number(text);
  if (field.type === 'int') {
    return Math.round(number);
  }
  if (field.type in LENGTH_UNITS && units === 'in') {
    return tidy(number * MM_PER_INCH);
  }
  return number;
}

function control(field, value, units, materials, placeholder, optional) {
  if (field.type === 'choice') {
    const options = field.options === 'materials' ? materials.map((name) => [name, name]) : field.options;
    const select = document.createElement('select');
    select.append(...options.map(([id, label]) => Object.assign(document.createElement('option'), { value: id, textContent: label })));
    select.value = value;
    return select;
  }
  const input = document.createElement('input');
  input.value = shown(field, value, units);
  input.required = !optional;
  input.placeholder = placeholder;
  if (field.type === 'list' || field.type === 'lengths') {
    input.pattern = String.raw`\s*\d*\.?\d+(\s*,\s*\d*\.?\d+)*\s*`;
    input.title = 'Numbers separated by commas';
    return input;
  }
  if (field.type === 'text') {
    return input;
  }
  input.type = 'number';
  input.step = field.type === 'int' ? '1' : 'any';
  input.min = field.type === 'int' ? '1' : '0';
  return input;
}

const has = (object, key) => Object.prototype.hasOwnProperty.call(object ?? {}, key);

// `setup` holds the form's choices besides the values: `materials`, the setup's `defaults`
// and, where known, the `computed` values the planner uses for entries left empty.
function row(field, values, units, setup) {
  const [section, key] = field.at.split('.');
  const hasDefault = has(setup.defaults[section], key);
  // An entry left empty takes the planner's computed value, or the default.
  const computed = setup.computed?.[section]?.[key];
  const fallback = computed ?? (hasDefault ? setup.defaults[section][key] : null);
  const placeholder = fallback === null || fallback === '' ? (field.placeholder ?? '') : shown(field, fallback, units);
  const optional = field.optional || (field.advanced && hasDefault);
  const input = control(field, values[section][key], units, setup.materials, placeholder, optional);
  input.name = field.at;
  // What the user was shown. Only a field they change is written back: a length
  // shown rounded in inches must not come back as a slightly different length.
  input.dataset.shown = input.value;
  const label = document.createElement('label');
  const unit = LENGTH_UNITS[field.type]?.[units] ?? field.unit ?? '';
  label.append(
    Object.assign(document.createElement('span'), { textContent: field.label }),
    input,
    Object.assign(document.createElement('small'), { textContent: unit }),
  );
  if (field.note) {
    label.append(Object.assign(document.createElement('em'), { textContent: field.note }));
  }
  return label;
}

// Whether a field holds something other than its default, so it must not stay hidden.
function changed(field, values, defaults) {
  const [section, key] = field.at.split('.');
  const fallback = has(defaults[section], key) ? defaults[section][key] : null;
  return JSON.stringify(values[section][key] ?? null) !== JSON.stringify(fallback);
}

// Fill `form` with the fields of one step: the ones to answer, then the advanced ones in a
// section that starts open when any of them differs from its default.
export function draw(form, step, values, units, setup) {
  const basic = step.fields.filter((field) => !field.advanced);
  const advanced = step.fields.filter((field) => field.advanced);
  form.replaceChildren(...basic.map((field) => row(field, values, units, setup)));
  if (advanced.length === 0) {
    return;
  }
  const details = document.createElement('details');
  details.className = 'advanced';
  details.open = advanced.some((field) => changed(field, values, setup.defaults));
  details.append(
    Object.assign(document.createElement('summary'), { textContent: 'Advanced' }),
    ...advanced.map((field) => row(field, values, units, setup)),
  );
  form.append(details);
}

// Open the advanced section when it holds an entry the browser would refuse, so the
// browser can point at it.
export function reveal(form) {
  for (const details of form.querySelectorAll('details')) {
    if (details.querySelector(':invalid')) {
      details.open = true;
    }
  }
}

// Copy what the form shows back into `values`.
export function read(form, step, values, units) {
  for (const field of step.fields) {
    const [section, key] = field.at.split('.');
    const input = form.elements[field.at];
    // Back and a change of units read a step without validating it: a half-typed or invalid
    // entry is dropped there, never stored.
    if (input.value === input.dataset.shown || !input.checkValidity()) {
      continue;
    }
    values[section][key] = typed(field, input.value, units);
    input.dataset.shown = input.value;
  }
}
