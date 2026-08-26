export function upgradeLocalPrivateReviewBundle(bundle) {
  const questionResults = bundle?.weekly_question_results;
  if (!questionResults) return bundle;
  if (questionResults.format_version === 'foldarium.weekly-question-results/v1') {
    questionResults.format_version = 'foldarium.weekly-question-results/v2';
  }
  const blindItems = new Map((bundle.blind_manifest?.items || []).map(item => [item.id, item]));
  const revealItems = new Map((bundle.reveal_manifest?.items || []).map(item => [item.id, item]));
  for (const result of questionResults.items || []) {
    for (const answer of result.answers || []) {
      answer.selection_kind = answer.picked_none ? 'none' : 'cluster';
    }
    if (result.answers?.some(answer => answer.display_names?.includes('Smina'))) continue;
    const blindItem = blindItems.get(result.item_id);
    const revealItem = revealItems.get(result.item_id);
    if (!blindItem || !revealItem) throw new Error('Local Smina result item is missing');
    const choices = [...blindItem.choices];
    choices.sort((left, right) => (
      left.smina_score.value - right.smina_score.value
      || left.id.localeCompare(right.id)
    ));
    const best = choices[0];
    const revealed = revealItem.choices.find(choice => choice.id === best.id);
    if (!revealed) throw new Error('Local Smina reveal choice is missing');
    let answer = result.answers.find(candidate => (
      candidate.picked_none === false
      && candidate.selection_kind === 'exact'
      && candidate.choice_id === best.id
    ));
    if (!answer) {
      answer = {
        choice_id: best.id,
        picked_none: false,
        selection_kind: 'exact',
        correct: revealed.correct === true,
        vote_count: 0,
        display_names: [],
      };
      result.answers.push(answer);
    }
    answer.vote_count += 1;
    answer.display_names.push('Smina');
    answer.display_names.sort((left, right) => left.localeCompare(right));
    result.answered_count += 1;
    if (answer.correct) {
      result.correct_count += 1;
      result.correct_display_names.push('Smina');
      result.correct_display_names.sort((left, right) => left.localeCompare(right));
    }
  }
  return bundle;
}
