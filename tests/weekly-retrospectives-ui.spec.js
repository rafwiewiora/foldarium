import { expect, test } from 'playwright/test';
import { readFileSync } from 'node:fs';

const baseUrl = process.env.RETROSPECTIVE_ARCHIVE_URL
  || 'http://127.0.0.1:4320/weekly/retrospectives';
const archiveHtml = readFileSync(new URL('../weekly-retrospectives.html', import.meta.url), 'utf8');
const roundId = 'weekly-2026-08-20';

const round = {
  round_id: roundId,
  campaign_id: 'weekly-main',
  opens_at: '2026-08-15T00:00:00Z',
  closes_at: '2026-08-19T00:00:00Z',
  revealed_at: '2026-08-20T00:00:00Z',
  blind_week: '2026-08-20',
  item_count: 2,
  choice_count: 3,
};

const listPayload = {
  format_version: 'foldarium.weekly-retrospective-list/v1',
  publications: [{
    ...round,
    summary: {
      human_participant_count: 4,
      human_complete_count: 3,
      human_partial_count: 1,
      automated_winner: {
        participant: 'Claude Opus',
        participant_kind: 'llm',
        correct: 2,
        total: 2,
      },
      outcomes: {
        pose_solved: 1,
        pose_unsolved: 1,
        none_solved: 0,
        none_unsolved: 0,
        suppressed: 0,
      },
    },
  }],
  next_cursor: null,
};

const blindItems = [
  {
    id: 'item-a',
    ligand: { component_id: 'LIG', heavy_atoms: 18 },
    week: '2026-08-20',
    protein_uri: 'https://example.supabase.co/storage/v1/object/public/structures/protein.pdb',
    metadata: {},
    choices: [
      { id: 'choice-a', pose_uri: 'https://example.supabase.co/storage/v1/object/public/structures/a.pdb' },
      { id: 'choice-b', pose_uri: 'https://example.supabase.co/storage/v1/object/public/structures/b.pdb' },
    ],
  },
  {
    id: 'item-b',
    ligand: { component_id: 'DRG', heavy_atoms: 21 },
    week: '2026-08-20',
    protein_uri: 'https://example.supabase.co/storage/v1/object/public/structures/protein.pdb',
    metadata: {},
    choices: [
      { id: 'choice-c', pose_uri: 'https://example.supabase.co/storage/v1/object/public/structures/c.pdb' },
    ],
  },
];

const detailPayload = {
  format_version: 'foldarium.weekly-retrospective-detail/v1',
  round,
  blind_manifest: { schema_version: 1, round_id: roundId, items: blindItems },
  reveal_manifest: {
    schema_version: 1,
    round_id: roundId,
    items: [
      {
        id: 'item-a',
        choices: [
          { id: 'choice-a', correct: true, accepted_correct: true, rmsd: 0.8 },
          { id: 'choice-b', correct: false, accepted_correct: false, rmsd: 2.8 },
        ],
      },
      {
        id: 'item-b',
        choices: [
          { id: 'choice-c', correct: true, accepted_correct: true, rmsd: 1.1 },
        ],
      },
    ],
  },
  retrospective: {
    human_aggregate: {
      participant_count: 4,
      suppressed: false,
      complete_count: 3,
      partial_count: 1,
      score_distribution: [],
    },
    automated_entries: [
      {
        participant: 'Claude Opus', participant_kind: 'llm', correct: 2,
        answered: 2, total: 2, accuracy: 100, coverage: 100, complete: true,
      },
      {
        participant: 'Smina', participant_kind: 'baseline', correct: 1,
        answered: 2, total: 2, accuracy: 50, coverage: 100, complete: true,
      },
    ],
    human_entries: [{
      participant: 'PocketFox', participant_kind: 'human', correct: 1,
      answered: 2, total: 2, accuracy: 50, coverage: 100, complete: true,
    }],
    questions: [
      {
        item_id: 'item-a',
        human_aggregate: {
          answered_count: 4,
          suppressed: false,
          correct_count: 2,
          answers: [{
            choice_id: 'choice-a', picked_none: false, selection_kind: 'exact',
            correct: true, vote_count: 2, display_names: ['PocketFox', 'PosePilot'],
          }],
        },
        automated_entries: [{
          participant: 'Claude Opus', participant_kind: 'llm', choice_id: 'choice-a',
          picked_none: false, selection_kind: 'exact', correct: true,
        }],
      },
      {
        item_id: 'item-b',
        human_aggregate: {
          answered_count: 2,
          suppressed: false,
          correct_count: 0,
          answers: [{
            choice_id: 'choice-c', picked_none: false, selection_kind: 'exact',
            correct: false, vote_count: 2, display_names: ['PocketFox', 'PosePilot'],
          }],
        },
        automated_entries: [{
          participant: 'Smina', participant_kind: 'baseline', choice_id: 'choice-c',
          picked_none: false, selection_kind: 'exact', correct: false,
        }],
      },
    ],
  },
};

const maliciousName = '<img src=x onerror="window.pwned=1">';
const adminDetail = {
  ...detailPayload,
  format_version: 'foldarium.weekly-retrospective-admin-detail/v1',
  retrospective: {
    participants: [{
      participant: maliciousName,
      participant_kind: 'human',
      correct: 1,
      answered: 2,
      total: 2,
      accuracy: 50,
      coverage: 100,
      complete: true,
    }],
    questions: [{
      item_id: 'item-a',
      responses: [{
        participant: maliciousName,
        participant_kind: 'human',
        choice_id: 'choice-a',
        picked_none: false,
        selection_kind: 'exact',
        correct: true,
      }],
    }],
  },
};

const publicAllTime = {
  participants: [
    {
      rank: 1,
      participant: 'Claude Opus',
      participant_kind: 'llm',
      weeks_participated: 4,
      complete_weeks: 4,
      total_correct: 7,
      total_questions: 8,
      weighted_average_accuracy: 87.5,
      provisional: false,
    },
    {
      rank: 2,
      participant: maliciousName,
      participant_kind: 'human',
      weeks_participated: 2,
      complete_weeks: 2,
      total_correct: 3,
      total_questions: 4,
      weighted_average_accuracy: 75,
      provisional: true,
    },
  ],
};

const adminAllTime = {
  participants: [{
    rank: 1,
    participant: maliciousName,
    participant_kind: 'human',
    weeks_participated: 2,
    complete_weeks: 2,
    total_correct: 3,
    total_questions: 4,
    weighted_average_accuracy: 75,
    provisional: true,
  }],
};

async function mockApi(page) {
  await page.route('**/weekly/retrospectives**', route => {
    if (route.request().resourceType() === 'document') {
      return route.fulfill({ contentType: 'text/html', body: archiveHtml });
    }
    return route.continue();
  });
  await page.route('**/api/weekly-retrospectives**', route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('round_id')) {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(url.searchParams.get('admin') === '1' ? adminDetail : detailPayload),
      });
    }
    if (url.searchParams.get('all_time') === '1') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(url.searchParams.get('admin') === '1' ? adminAllTime : publicAllTime),
      });
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(listPayload) });
  });
  await page.route('**/api/weekly-play-for-fun-results**', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      format_version: 'foldarium.weekly-play-for-fun-leaderboard/v1',
      round_id: roundId,
      item_count: 1,
      participant_count: 1,
      complete_runs: [{
        display_name: 'Playful Player',
        correct: 1,
        answered: 1,
        total: 1,
        accuracy: 100,
        coverage: 100,
        participation_mode: 'for_fun',
        rank: 1,
      }],
      partial_runs: [],
    }),
  }));
}

async function unlock(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#archive-app')).toBeVisible();
}

test('archive list has four outcome lanes, no Mol-star, and no desktop overflow', async ({ page }) => {
  const molecularRequests = [];
  page.on('request', request => {
    if (/molstar/i.test(request.url())) molecularRequests.push(request.url());
  });
  await mockApi(page);
  await unlock(page, baseUrl);
  await expect(page.locator('.round-row')).toHaveCount(1);
  await expect(page.locator('#round-list .round-date')).toContainText(
    'Blind week · Aug 20, 2026',
  );
  await expect(page.locator('.outcome-caption')).toHaveText(
    'Human outcomes · share of questions',
  );
  await expect(page.locator('.round-row .rail-lane')).toHaveCount(4);
  const filledWidth = await page.locator(
    '.rail-lane[data-outcome="pose-solved"] .rail-fill',
  ).evaluate(node => node.getBoundingClientRect().width);
  expect(filledWidth).toBeGreaterThan(0);
  await expect(page.locator('.round-row')).toContainText('Claude Opus');
  await expect(page.locator('.round-row')).not.toContainText('hidden for privacy');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(molecularRequests).toEqual([]);
});

test('desktop split view keeps archive summary inside its pane', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await mockApi(page);
  await unlock(page, `${baseUrl}/${roundId}`);
  await expect(page.locator('#round-detail')).toBeVisible();
  await expect(page.locator('.detail-head')).toContainText('Blind week');
  await expect(page.locator('.detail-head')).toContainText('Thursday, August 20, 2026');

  const layout = await page.evaluate(() => {
    const list = document.querySelector('#round-list').getBoundingClientRect();
    const detail = document.querySelector('#round-detail').getBoundingClientRect();
    const explore = document.querySelector('.round-row .explore').getBoundingClientRect();
    return {
      noPageOverflow: document.documentElement.scrollWidth <= innerWidth,
      panesSeparated: list.right < detail.left,
      exploreContained: explore.left >= list.left && explore.right <= list.right,
    };
  });

  expect(layout).toEqual({
    noPageOverflow: true,
    panesSeparated: true,
    exploreContained: true,
  });
});

test('detail filters four outcomes, safely renders admin names, and fits mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await unlock(page, `${baseUrl}/${roundId}`);
  await expect(page.locator('#round-detail')).toBeVisible();
  await expect(page.locator('.filter-row button')).toHaveCount(5);
  await expect(page.locator('.question-list')).toContainText('PocketFox');
  await expect(page.locator('.question-list')).toContainText('Human players');
  await expect(page.locator('.question-list')).toContainText('0/2 correct');
  await expect(page.locator('.question-list')).toContainText('Automated methods');
  await page.locator('.filter-row button[data-filter="pose-solved"]').click();
  await expect(page.locator('.question-row')).toHaveCount(1);
  await expect(page.locator('.question-row')).toContainText('LIG');
  await expect(page.locator('.admin-panel')).toContainText(maliciousName);
  await expect(page.locator('.admin-panel img')).toHaveCount(0);
  expect(await page.evaluate(() => window.pwned)).toBeUndefined();
  await expect(page.getByRole('link', { name: 'Play for fun' })).toHaveAttribute(
    'href',
    `/weekly?retrospective_round=${roundId}&play_for_fun=1`,
  );
  await expect(page.getByRole('link', { name: 'Open molecular review' })).toHaveAttribute(
    'href',
    `/weekly?retrospective_round=${roundId}`,
  );
  await expect(page.locator('.detail-section').filter({
    has: page.getByRole('heading', { name: 'Weekly player leaderboard' }),
  })).toContainText('Playful Player · For fun');
  await page.locator('#choose-round').click();
  await expect(page.locator('#round-chooser')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('all-time exposes public Human pseudonyms and marks provisional rows', async ({ page }) => {
  await mockApi(page);
  await unlock(page, `${baseUrl}?view=all-time`);
  const human = page.locator('#participant-filter [data-kind="human"]');
  await expect(human).toBeEnabled();
  await human.click();
  await expect(page.locator('.ranking-table')).toContainText(maliciousName);
  await expect(page.locator('.ranking-table')).toContainText('Provisional');
  await expect(page.locator('.ranking-table img')).toHaveCount(0);
});
