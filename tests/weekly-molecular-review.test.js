import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { sortWeeklySimilarityRows } from '../weekly-training-similarity.js';

const root = new URL('../', import.meta.url);
const source = name => readFile(new URL(name, root), 'utf8');

test('molecular navigation composes outcome filtering with stable similarity sorting', () => {
  const rows = [
    { index: 0, publicationIndex: 0, outcome: 'pose', similarity: {
      classification: 'familiar', train_shape_overlap: 0.8,
    } },
    { index: 1, publicationIndex: 1, outcome: 'none', similarity: {
      classification: 'novel', train_shape_overlap: 0.1,
    } },
    { index: 2, publicationIndex: 2, outcome: 'pose', similarity: {
      classification: 'novel', train_shape_overlap: null,
    } },
    { index: 3, publicationIndex: 3, outcome: 'pose', similarity: null },
  ];
  const poseRows = rows.filter(row => row.outcome === 'pose');

  assert.deepEqual(
    sortWeeklySimilarityRows(poseRows, 'default').map(row => row.index),
    [0, 2, 3],
  );
  assert.deepEqual(
    sortWeeklySimilarityRows(poseRows, 'novel-first').map(row => row.index),
    [2, 0, 3],
  );
  assert.deepEqual(
    sortWeeklySimilarityRows(poseRows, 'familiar-first').map(row => row.index),
    [0, 2, 3],
  );
  assert.deepEqual(rows.map(row => row.index), [0, 1, 2, 3]);
});

test('archive bootstrap fetches similarity fail-open and joins only exact week/item records', async () => {
  const [index, app] = await Promise.all([source('index.html'), source('app.js')]);

  assert.match(
    index,
    /archiveRoundId && !archivePlayForFun\s*\?\s*fetchWeeklyTrainingSimilarityReport\(\)\.catch/,
  );
  assert.match(index, /return null;/);
  assert.match(index, /window\.foldariumWeeklyTrainingSimilarity = Object\.freeze/);
  assert.match(app, /similarityFor\(similarityReport, detail\.round\.blind_week, item\.id\)/);
  assert.match(app, /normalizedPool\.map\(\(item, publicationIndex\) => \(\{/);
  assert.doesNotMatch(app, /\.sort\([^)]*ITEMS|ITEMS\.sort|POOLS\.weekly\.sort/);
});

test('closest training is a reference pose with no toggle or scoring semantics', async () => {
  const [index, app] = await Promise.all([source('index.html'), source('app.js')]);

  assert.doesNotMatch(index, /id="closest-training(?:-control|-status)?"/);
  assert.doesNotMatch(app, /showClosestTraining|closestTrainingPending|closest_training_toggled/);
  assert.match(app, /_trainingReference: true/);
  assert.match(app, /_weeklyChoiceId: '__training_reference__'/);
  assert.match(app, /training_score: Number\.isFinite\(similarity\.train_shape_overlap\)/);
  assert.match(app, /correct: false,\s*clusterAccepted: false,\s*rmsd: null,/s);
});

test('closest training renders only the pre-aligned scored ligand as another pose', async () => {
  const app = await source('app.js');
  const start = app.indexOf('async function addTrainingReferencePose');
  const end = app.indexOf('async function clearViewerScene', start);
  const helper = app.slice(start, end);

  assert.match(helper, /loadStruct\(choice\.pose_file, 'pdb', targetPlugin\)/);
  assert.match(helper, /addClosestTrainingLigand\(\s*loaded\.struct,\s*choice\.training_ligand/s);
  assert.match(app, /tryCreateComponentStatic\(\s*struct,\s*'ligand'/s);
  assert.match(app, /type: 'ball-and-stick'/);
  assert.match(helper, /polymer is deliberately not represented/);
  assert.doesNotMatch(helper, /'polymer',\s*'cartoon'/);
  assert.doesNotMatch(helper, /rigidPdbTransform/);
});

test('training reference is available in One and Grid but omitted from Show all', async () => {
  const [index, app] = await Promise.all([source('index.html'), source('app.js')]);
  const nav = app.slice(
    app.indexOf('function retrospectiveNavChoices()'),
    app.indexOf('const viewingReleasedCrystal'),
  );
  const gridEntries = app.slice(
    app.indexOf('function gridEntriesFor(method)'),
    app.indexOf('function weeklyGridPage()'),
  );
  const canonical = app.slice(
    app.indexOf('async function buildRetrospectiveCanonicalLayer'),
    app.indexOf('async function buildCanonicalLayer'),
  );

  assert.match(app, /if \(training\) references\.push\(training\)/);
  assert.ok(
    nav.indexOf('references.push(buildXtalReferenceChoice') < nav.indexOf('references.push(training)'),
    'One should place Xtal before closest training',
  );
  assert.ok(
    gridEntries.indexOf('choice: buildXtalReferenceChoice') < gridEntries.indexOf('choice: training'),
    'Grid should place Xtal before closest training',
  );
  assert.match(app, /trainingReference: true/);
  assert.match(app, /async function buildRetrospectiveTrainingGridCell/);
  assert.match(app, /isTrainingReferenceChoice\(shown\[0\]\)[\s\S]*?buildRetrospectiveTrainingLayer/);
  assert.doesNotMatch(canonical, /displayMode === 'all'[\s\S]{0,300}addTrainingReferencePose/);
  assert.match(app, /Closest training · \$\{source\} · overlap \$\{score\}/);
  assert.match(app, /<span class="grid-title">Closest training<\/span>/);
  assert.match(app, /trainingReferenceAnnotation\(choice\).*not scored/s);
  assert.match(
    app,
    /const modehint = \$\('#modehint'\);[\s\S]*modehint\.textContent = '';[\s\S]*modehint\.style\.display = 'none';/,
  );
  assert.match(index, /\.badge\{[^}]*white-space:normal;overflow-wrap:anywhere;/s);
});

test('training-pose load errors remove only their own data and leave Xtal state independent', async () => {
  const app = await source('app.js');
  const start = app.indexOf('async function addTrainingReferencePose');
  const end = app.indexOf('async function clearViewerScene', start);
  const helper = app.slice(start, end);

  assert.match(helper, /update\.delete\(loaded\.data\.ref \|\| loaded\.data\)/);
  assert.match(helper, /Closest training pose omitted/);
  assert.match(helper, /return null/);
  assert.doesNotMatch(helper, /clearViewerScene|showXtal|releasedCrystalMode|answer_crystal_pdb/);
});
