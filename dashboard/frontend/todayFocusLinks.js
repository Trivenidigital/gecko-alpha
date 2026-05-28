const KNOWN_DEXSCREENER_CHAINS = new Set([
  'arbitrum',
  'avalanche',
  'base',
  'bsc',
  'ethereum',
  'polygon',
  'solana',
])

export function isContractAddress(tokenId) {
  return Boolean(
    tokenId && (tokenId.startsWith('0x') || /^[1-9A-HJ-NP-Za-km-z]{32,}$/.test(tokenId))
  )
}

export function researchLinks(row) {
  const tokenId = row?.token_id || ''
  if (!tokenId) return { chartHref: null, cgHref: null }
  const encodedToken = encodeURIComponent(tokenId)
  const chain = String(row?.chain || 'coingecko').toLowerCase()
  const encodedChain = encodeURIComponent(chain)
  const contract = isContractAddress(tokenId)
  const chartIsDirect = contract && KNOWN_DEXSCREENER_CHAINS.has(chain)
  const cgIsDirect = !contract
  const chartHref = chartIsDirect
    ? `https://dexscreener.com/${encodedChain}/${encodedToken}`
    : `https://dexscreener.com/search?q=${encodedToken}`
  const cgHref = cgIsDirect
    ? `https://www.coingecko.com/en/coins/${encodedToken}`
    : `https://www.coingecko.com/en/search?query=${encodedToken}`
  return {
    chartHref,
    chartLabel: chartIsDirect ? 'Chart' : 'Dex search',
    cgHref,
    cgLabel: cgIsDirect ? 'CG' : 'CG search',
  }
}
