import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ReadingSummary } from '../ReadingSummary'

describe('ReadingSummary', () => {
  afterEach(() => cleanup())

  it('keeps a structured key-point title when points is missing', () => {
    expect(() => {
      render(
        <ReadingSummary
          keyPoints={[{ title: '只有小节标题' }]}
          testIds={{ container: 'summary', keyPoints: 'key-points' }}
        />,
      )
    }).not.toThrow()

    expect(screen.getByText('只有小节标题')).toBeInTheDocument()
  })
})
