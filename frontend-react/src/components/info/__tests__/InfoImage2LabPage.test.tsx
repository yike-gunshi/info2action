import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { InfoImage2LabPage } from '../InfoImage2LabPage'

describe('InfoImage2LabPage', () => {
  afterEach(() => cleanup())

  it('渲染独立 Image2 信息页原型，不复用旧信息流组件 testid', () => {
    render(<InfoImage2LabPage />)

    expect(screen.getByTestId('image2-lab-page')).toBeInTheDocument()
    expect(screen.getByTestId('image2-lab-topbar')).toBeInTheDocument()
    expect(screen.getByTestId('image2-lab-logo')).toHaveTextContent('info2act')
    expect(screen.getByTestId('image2-lab-segmented')).toHaveTextContent('按分类')
    expect(screen.getByTestId('image2-lab-category-rail')).toHaveTextContent('产品')
    expect(screen.getAllByTestId('image2-lab-column')).toHaveLength(3)
    expect(screen.getAllByTestId('image2-lab-card')).toHaveLength(12)

    expect(screen.queryByTestId('info-view')).not.toBeInTheDocument()
    expect(screen.queryByTestId('info-subbar')).not.toBeInTheDocument()
    expect(screen.queryByTestId('info-card')).not.toBeInTheDocument()
  })

  it('使用真实 item 风格样例，摘要区只保留 AI 总结正文', () => {
    render(<InfoImage2LabPage />)

    expect(screen.getByText('tinyhumansai/openhuman')).toBeInTheDocument()
    expect(screen.getByText('We let AIs run radio stations')).toBeInTheDocument()
    expect(screen.getByText('Figma Sites 正式上线：从设计到发布，一体化网页构建工具')).toBeInTheDocument()
    expect(screen.queryByText('摘要')).not.toBeInTheDocument()
    expect(screen.queryByText(/AI 速览/)).not.toBeInTheDocument()
    expect(screen.queryByText('✦')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('image2-lab-card-summary')[0]).toHaveClass('line-clamp-4')
  })

  it('无图 item 不固定高度，平台作者在左下角，事件信息在右下角', () => {
    render(<InfoImage2LabPage />)

    const firstCard = screen.getAllByTestId('image2-lab-card')[0]
    expect(firstCard).toHaveAttribute('data-has-cover', 'false')
    expect(firstCard.className).not.toContain('min-h')
    expect(within(firstCard).queryByRole('img')).not.toBeInTheDocument()

    const text = firstCard.textContent ?? ''
    expect(text.indexOf('OpenHuman 是')).toBeLessThan(text.indexOf('GitHub'))
    expect(within(firstCard).getByTestId('image2-lab-card-source')).toHaveTextContent('GitHub')
    expect(within(firstCard).getByTestId('image2-lab-card-source')).toHaveTextContent('tinyhumansai')
    expect(within(firstCard).getByTestId('image2-lab-card-events')).toHaveTextContent('1.7w')
    expect(within(firstCard).getByTestId('image2-lab-card-events')).toHaveTextContent('1.5k')
    expect(within(firstCard).getByTestId('image2-lab-card-events')).toHaveTextContent('1 天前')
  })

  it('有图 item 使用 21:9 封面比例', () => {
    render(<InfoImage2LabPage />)

    const covers = screen.getAllByTestId('image2-lab-card-cover')
    expect(covers).not.toHaveLength(0)
    expect(covers[0]).toHaveClass('aspect-[21/9]')
  })
})
