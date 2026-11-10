// Setup wizard: the session's setup as five short forms. Values are kept in millimetres, as
// the setup file has them; on a machine in inches they are only shown and typed in inches.

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
      { at: 'machine.name', label: 'Name', type: 'text', optional: true },
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
      { at: 'tool.holder', label: 'Holder', type: 'text', optional: true },
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
      { at: 'cut.width_max_mm', label: 'Widest radial cut to try', type: 'length', optional: true, placeholder: 'half the diameter' },
      { at: 'cut.chip_load_mm', label: 'Chip load per tooth', type: 'length', optional: true, placeholder: 'from the material' },
      { at: 'cut.speed_min_rpm', label: 'Slowest speed to try', type: 'int', unit: 'rpm', optional: true, placeholder: 'a quarter of the fastest' },
      { at: 'cut.speed_max_rpm', label: 'Fastest speed to try', type: 'int', unit: 'rpm', optional: true, placeholder: 'machine or material limit' },
    ],
  },
  {
    title: 'Stock and program',
    hint: 'A block the test can cut up, clamped low and rigidly.',
    fields: [
      { at: 'stock.length_mm', label: 'Stock length (X)', type: 'length', note: 'Every pass cuts the full length.' },
      { at: 'stock.width_mm', label: 'Stock width to use (Y)', type: 'length', note: 'Material the test may consume from the front face.' },
      { at: 'program.coolant', label: 'Coolant', type: 'choice', options: [['none', 'none'], ['flood', 'flood'], ['mist', 'mist']], note: 'Coolant noise makes chatter harder to hear.' },
      { at: 'program.dwell_s', label: 'Pause at speed before each pass', type: 'number', unit: 's' },
      { at: 'program.clearance_mm', label: 'Clearance around the stock', type: 'length' },
      { at: 'program.safe_z_mm', label: 'Safe height (Z)', type: 'length' },
      { at: 'program.plunge_feed_mm_min', label: 'Plunge feed', type: 'feed' },
      { at: 'program.truing_width_mm', label: 'Truing pass width', type: 'length' },
    ],
  },
  {
    title: 'Planner',
    hint: 'How boldly the next cuts are chosen. The defaults suit most sessions.',
    fields: [
      { at: 'planner.round1_speeds', label: 'Speeds in round 1', type: 'int' },
      { at: 'planner.round1_width_fractions', label: 'Round 1 widths', type: 'list', note: 'Fractions of the widest radial cut, separated by commas.' },
      { at: 'planner.batch_size', label: 'Most cuts in a later round', type: 'int' },
      { at: 'planner.min_probability', label: 'Chance of being stable a cut needs', type: 'number', note: 'Assuming the cut before it passes. Between 0 and 1.' },
      { at: 'planner.probability_floor', label: 'The same, on measured cuts alone', type: 'number' },
      { at: 'planner.stop_improvement', label: 'Stop when a cut would add less than', type: 'number', note: 'As a fraction of the best removal rate so far.' },
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

function control(field, value, units, materials) {
  if (field.type === 'choice') {
    const options = field.options === 'materials' ? materials.map((name) => [name, name]) : field.options;
    const select = document.createElement('select');
    select.append(...options.map(([id, label]) => Object.assign(document.createElement('option'), { value: id, textContent: label })));
    select.value = value;
    return select;
  }
  const input = document.createElement('input');
  input.value = shown(field, value, units);
  input.required = !field.optional;
  input.placeholder = field.placeholder ?? '';
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

// Fill `form` with the fields of one step.
export function draw(form, step, values, units, materials) {
  form.replaceChildren(
    ...step.fields.map((field) => {
      const [section, key] = field.at.split('.');
      const input = control(field, values[section][key], units, materials);
      input.name = field.at;
      // What the user was shown. Only a field they change is written back: a length
      // shown rounded in inches must not come back as a slightly different length.
      input.dataset.shown = input.value;
      const row = document.createElement('label');
      const unit = LENGTH_UNITS[field.type]?.[units] ?? field.unit ?? '';
      row.append(
        Object.assign(document.createElement('span'), { textContent: field.label }),
        input,
        Object.assign(document.createElement('small'), { textContent: unit }),
      );
      if (field.note) {
        row.append(Object.assign(document.createElement('em'), { textContent: field.note }));
      }
      return row;
    }),
  );
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
