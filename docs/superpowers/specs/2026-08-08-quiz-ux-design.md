# Quiz UX Design

## Goal

Make the quiz intuitive for a mixed scientific audience, including first-time users, while keeping expert viewer controls available. Replay is out of scope.

## Direction

Use a minimal three-state flow with progressively disclosed technical detail:

1. Choose dataset and difficulty.
2. Inspect the molecular viewer.
3. Select a pose.
4. Submit the answer.
5. Review a concise result and continue.

## Interface

- **Setup:** show only Dataset, Difficulty, and **Start quiz**, with short option descriptions.
- **Question:** show progress, score, viewer, pose choices, and **Submit answer**.
- **Result:** lead with **Correct** or **Not quite**, then show **Next question** as the primary action.
- Keep one instruction: **Pick the pose that best fits the binding pocket.**
- Put view mode, protein source, clustering, and H-bonds inside a collapsed **View options** section.
- Put RMSD values, AI comparison, and answer-inspection controls inside collapsed **Answer details**.
- Give the selected pose a strong border, checkmark, and **Selected** label.
- Remove tutorials, persistent technical explanations, and competing primary actions.
- Keep scientific dataset names, but pair them with concise plain-language descriptions.

## Responsive and Accessible Behavior

- On narrow screens, stack the viewer above a bottom control panel instead of retaining the fixed sidebar.
- Expose selected segmented controls with `aria-pressed`.
- Announce loading and answer results with live regions.
- Preserve visible keyboard focus and existing arrow-key viewer navigation.
- Keep touch targets at least 40 pixels high.

## Implementation Constraints

- Keep the existing static HTML and JavaScript architecture.
- Prefer CSS and small DOM changes over new abstractions or dependencies.
- Preserve quiz scoring, persistence, structure loading, and scientific answer logic.
- Do not modify replay mode.

## Verification

- Run the existing test suite.
- Walk through setup, selection, submission, answer review, and next-question flow.
- Check both CAMEO and Runs-n-Poses, Easy and Hard.
- Check desktop and a narrow mobile viewport.
- Verify keyboard focus, selected states, and live result text.
