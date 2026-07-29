import { describe, expect, it } from 'vitest'

import { selectRelatedLessons } from './reflections'
import type { LessonProposal } from './types'

function lesson(id: string, sourceReflectionIds: string[]): LessonProposal {
  return {
    id,
    title: id,
    status: 'candidate',
    summary: id,
    independent_episode_count: 1,
    support_count: 1,
    counterexample_count: 0,
    source_reflection_ids: sourceReflectionIds,
    half_life_sessions: 60,
    replay_target_dates: 0,
    replay_batch_count: 0,
    wiki_review_ready: null,
    replay_blockers: [],
    revalidation_due: false,
    revalidation_due_reasons: [],
    lifecycle_history: [],
  }
}

describe('selectRelatedLessons', () => {
  it('does not fall back to unrelated global lessons for a selected reflection', () => {
    const globalOnly = lesson('LESSON-OTHER', ['REF-OTHER'])

    expect(selectRelatedLessons([globalOnly], {
      id: 'REF-SELECTED',
      lesson_candidate_ids: [],
    })).toEqual([])
  })

  it('keeps candidate-id and source-reflection matches', () => {
    const byCandidate = lesson('LESSON-CANDIDATE', ['REF-OTHER'])
    const bySource = lesson('LESSON-SOURCE', ['REF-SELECTED'])

    expect(selectRelatedLessons([byCandidate, bySource], {
      id: 'REF-SELECTED',
      lesson_candidate_ids: ['LESSON-CANDIDATE'],
    })).toEqual([byCandidate, bySource])
  })
})
