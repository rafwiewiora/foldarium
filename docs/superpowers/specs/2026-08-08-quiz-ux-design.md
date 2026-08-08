# Quiz UX Design

## Goal

Make the quiz intuitive for a mixed scientific audience, including first-time users, while keeping expert viewer controls available. Replay is out of scope.

## Direction

Use a guided core flow with progressively disclosed viewer controls:

1. Choose dataset and difficulty.
2. Inspect the molecular viewer.
3. Select a pose.
4. Submit the answer.
5. Review a concise result and continue.

## Interface

- Label setup groups as **Dataset** and **Difficulty**, with short plain-language descriptions.
- Show question progress and score together near the top of the sidebar.
- Keep pose choices and the primary action visually dominant.
- Rename **Lock in answer** to **Submit answer**.
- Put view mode, protein source, clustering, and H-bonds inside a collapsed **Viewer tools** section.
- Replace the persistent technical paragraph with short contextual guidance.
- Give the selected pose a strong border, checkmark, and **Selected** label.
- After submission, lead with **Correct** or **Not quite**, followed by the correct pose and RMSD details.
- Make **Next question** the only primary result action; keep answer-inspection controls secondary.
- Add a short first-question tip explaining rotation, zoom, pose colors, and submission.

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
