const markOnPaper = "/brand/mark-onwhite.svg";
const markOnDark = "/brand/mark-reverse.svg";
const wordmarkOnPaper = "/brand/wordmark-violet.svg";
const wordmarkOnDark = "/brand/wordmark-cream.svg";

// 两种色板使用同一套已锁定矢量资产，只有配色版本不同。
export function OfficialMark({className = ""}: {className?: string}) {
  return <span className={`official-mark ${className}`} role="img" aria-label="水木验码徽章">
    <img className="brand-on-paper" src={markOnPaper} alt="" />
    <img className="brand-on-dark" src={markOnDark} alt="" />
  </span>;
}

export function OfficialLockup({compact = false, context}: {compact?: boolean; context?: string}) {
  return <div className={`official-lockup${compact ? " compact" : ""}`}>
    <OfficialMark />
    <span className="official-divider" aria-hidden="true" />
    <div className="official-copy">
      <h1>
        <img className="brand-on-paper" src={wordmarkOnPaper} alt="水木验码" />
        <img className="brand-on-dark" src={wordmarkOnDark} alt="水木验码" />
        {context && <span className="official-context">{context}</span>}
      </h1>
      <p>SHUIMU YANMA</p>
    </div>
  </div>;
}
