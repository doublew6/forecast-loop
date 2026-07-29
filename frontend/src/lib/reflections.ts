import type { LessonProposal, ReflectionDetail } from './types'

export function selectRelatedLessons(
  lessons: LessonProposal[],
  reflection?: Pick<ReflectionDetail, 'id' | 'lesson_candidate_ids'>,
  limit = 6,
): LessonProposal[] {
  if (!reflection) return lessons.slice(0, limit)
  const candidateIds = new Set(reflection.lesson_candidate_ids)
  return lessons
    .filter(
      (lesson) => (
        candidateIds.has(lesson.id)
        || lesson.source_reflection_ids.includes(reflection.id)
      ),
    )
    .slice(0, limit)
}
