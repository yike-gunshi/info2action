import { useActionStore } from './actionStore'
import { useDetailStore } from './detailStore'
import { useFeedStore } from './feedStore'
import { useUIStore } from './uiStore'

export function resetClientSessionState() {
  useDetailStore.getState().closeModal()
  useDetailStore.setState({ detailCache: new Map() })

  useActionStore.setState({
    actions: [],
    counts: {},
    directions: [],
    isLoading: false,
  })

  useFeedStore.setState({
    sectionItems: new Map(),
    catCounts: {},
    searchResults: null,
    searchTotal: 0,
    searchCatCounts: {},
    searchPlatformSectionItems: null,
    searchPlatformCounts: {},
    searchSourceCounts: {},
    searchPlatformCategoryCounts: {},
    searchPlatformLoading: false,
    isSearching: false,
    isLoading: false,
    loadError: null,
    platformSectionItems: new Map(),
    platformCounts: {},
    sourceCounts: {},
    clickedAtById: {},
    isFetching: false,
  })

  useUIStore.setState({
    // v18.0 nav-merge: 默认 tab 改为 highlights（recommend tab 已删；与 uiStore.ts 默认一致）
    l1: 'highlights',
    expandedKey: null,
    searchQuery: '',
  })
}
