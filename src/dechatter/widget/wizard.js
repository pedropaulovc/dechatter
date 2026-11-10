// Setup wizard: the session's setup as four short forms. Values are kept in millimetres, as
// the setup file has them; with inch units chosen they are only shown and typed in inches.

const MM_PER_INCH = 25.4;
const LENGTH_UNITS = { length: { mm: 'mm', in: 'in' }, feed: { mm: 'mm/min', in: 'in/min' } };

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
    hint: 'The test holds the depth and steps the spindle speed and the radial width.',
    fields: [
      { at: 'cut.material', label: 'Work material', type: 'choice', options: 'materials' },
      { at: 'cut.axial_depth_mm', label: 'Axial depth of cut', type: 'length' },
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
];

const tidy = (number) => Number(number.toFixed(4));

function shown(field, value, units) {
  if (value === null || value === undefined) {
    return '';
  }
  if (field.type in LENGTH_UNITS && units === 'in') {
    return String(tidy(value / MM_PER_INCH));
  }
  return String(value);
}

function typed(field, text, units) {
  if (field.type === 'text' || field.type === 'choice') {
    return text.trim();
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
    values[section][key] = typed(field, form.elements[field.at].value, units);
  }
}
