const assert = require('node:assert/strict');
const resolveConfig = require('../app.config.js');
const baseConfig = require('../app.json').expo;
const projectId = 'f47ac10b-58cc-4372-a567-0e02b2c3d479';

process.env.EXPO_PUBLIC_EAS_PROJECT_ID = projectId;
const resolved = resolveConfig({ config: baseConfig });
assert.equal(resolved.extra.eas.projectId, projectId);

delete process.env.EXPO_PUBLIC_EAS_PROJECT_ID;
assert.equal(resolveConfig({ config: baseConfig }), baseConfig);

process.env.EXPO_PUBLIC_EAS_PROJECT_ID = 'not-a-project-id';
assert.throws(() => resolveConfig({ config: baseConfig }), /project UUID/);
delete process.env.EXPO_PUBLIC_EAS_PROJECT_ID;

process.stdout.write('Expo dynamic config passed\n');
