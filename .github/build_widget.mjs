// Build web/, the npm package's content, from an empty folder: `dechatter widget` only
// writes the files it knows, so anything left from an earlier build would be published too.
import { execFileSync } from 'node:child_process';
import { rmSync } from 'node:fs';

rmSync('web', { recursive: true, force: true });
execFileSync('uv', ['run', '--locked', 'dechatter', 'widget', 'web'], { stdio: 'inherit' });
