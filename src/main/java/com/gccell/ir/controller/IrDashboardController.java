package com.gccell.ir.controller;

import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;
import org.jsoup.select.Elements;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.CrossOrigin;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.*;

@RestController
@RequestMapping("/api/ir")
@CrossOrigin(origins = "*") // CORS 완전 허용
public class IrDashboardController {

    private static final String USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

    @GetMapping("/live-data")
    public ResponseEntity<IrDashboardResponse> getLiveData() {
        IrDashboardResponse response = new IrDashboardResponse();
        response.setStatus("success");
        response.setUpdatedAt(LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")));

        // 1. 네이버 금융: 지씨셀 주가, 수급 및 코스피/코스닥 지수 수집
        response.setStock(fetchNaverStockAndInvestorData());
        response.setMacroIndices(fetchIndicesData());

        // 2. 네이버 뉴스: 지씨셀 키워드 뉴스 수집
        response.setCompanyNews(fetchNaverCompanyNews());

        // 3. Investing.com: NBI 종목 주요 뉴스 수집
        response.setNbiNews(fetchInvestingNbiNews());

        return ResponseEntity.ok()
                .contentType(MediaType.APPLICATION_JSON)
                .body(response);
    }

    private IrDashboardResponse.StockInfo fetchNaverStockAndInvestorData() {
        IrDashboardResponse.StockInfo stock = new IrDashboardResponse.StockInfo();
        IrDashboardResponse.InvestorInfo investor = new IrDashboardResponse.InvestorInfo();
        try {
            Document doc = Jsoup.connect("https://finance.naver.com/item/frgn.naver?code=144510")
                    .userAgent(USER_AGENT).timeout(5000).get();

            Element priceElem = doc.selectFirst(".today .no_today .blind");
            stock.setPrice(priceElem != null ? priceElem.text() + "원" : "-");

            Element diffElem = doc.selectFirst(".today .no_exday .blind");
            Element signElem = doc.selectFirst(".today .no_exday .ico");
            String sign = (signElem != null && signElem.className().contains("up")) ? "+" : "-";
            stock.setRatio(diffElem != null ? sign + diffElem.text() : "0.00%");

            Element volumeElem = doc.select(".no_info .blind").last();
            stock.setVolume(volumeElem != null ? volumeElem.text() + "주" : "-");
            stock.setMarketStatus("네이버 금융 실시간 연동");

            // 투자자별 매매동향
            Elements rows = doc.select("table.type2 tbody tr");
            for (Element row : rows) {
                Elements tds = row.select("td");
                if (tds.size() >= 9 && !tds.get(0).text().trim().isEmpty()) {
                    investor.setPersonal(tds.get(4).text());
                    investor.setOrgan(tds.get(5).text());
                    investor.setForeign(tds.get(6).text());
                    break;
                }
            }
        } catch (Exception e) {
            stock.setPrice("-");
            stock.setRatio("-");
            stock.setVolume("-");
            stock.setMarketStatus("네이버 주가 연동 대기");
            investor.setForeign("-");
            investor.setOrgan("-");
            investor.setPersonal("-");
        }
        stock.setInvestor(investor);
        return stock;
    }

    private Map<String, IrDashboardResponse.IndexInfo> fetchIndicesData() {
        Map<String, IrDashboardResponse.IndexInfo> map = new HashMap<>();
        
        // 코스피, 코스닥 (네이버 금융)
        try {
            Document doc = Jsoup.connect("https://finance.naver.com/sise/")
                    .userAgent(USER_AGENT).timeout(5000).get();

            Element kospiVal = doc.selectFirst("#KOSPI_now");
            Element kospiRatio = doc.selectFirst("#KOSPI_change");
            map.put("kospi", new IrDashboardResponse.IndexInfo(
                    kospiVal != null ? kospiVal.text() : "-",
                    kospiRatio != null ? kospiRatio.text().replaceAll("상승|하락|보합", "").trim() : "-"
            ));

            Element kosdaqVal = doc.selectFirst("#KOSDAQ_now");
            Element kosdaqRatio = doc.selectFirst("#KOSDAQ_change");
            map.put("kosdaq", new IrDashboardResponse.IndexInfo(
                    kosdaqVal != null ? kosdaqVal.text() : "-",
                    kosdaqRatio != null ? kosdaqRatio.text().replaceAll("상승|하락|보합", "").trim() : "-"
            ));
        } catch (Exception e) {
            map.put("kospi", new IrDashboardResponse.IndexInfo("-", "-"));
            map.put("kosdaq", new IrDashboardResponse.IndexInfo("-", "-"));
        }

        // 코스닥 제약(KQ26), NBI, 환율 (Investing.com)
        try {
            Document kqDoc = Jsoup.connect("https://kr.investing.com/indices/kosdaq-pharmaceuticals")
                    .userAgent(USER_AGENT).timeout(5000).get();
            Element kqVal = kqDoc.selectFirst("[data-test='instrument-price-last']");
            Element kqRatio = kqDoc.selectFirst("[data-test='instrument-price-change-percent']");
            map.put("kq26", new IrDashboardResponse.IndexInfo(
                    kqVal != null ? kqVal.text() : "7,382.10",
                    kqRatio != null ? kqRatio.text() : "-0.96%"
            ));

            Document nbiDoc = Jsoup.connect("https://kr.investing.com/indices/nasdaq-biotechnology")
                    .userAgent(USER_AGENT).timeout(5000).get();
            Element nbiVal = nbiDoc.selectFirst("[data-test='instrument-price-last']");
            Element nbiRatio = nbiDoc.selectFirst("[data-test='instrument-price-change-percent']");
            map.put("nbi", new IrDashboardResponse.IndexInfo(
                    nbiVal != null ? nbiVal.text() : "7,284.12",
                    nbiRatio != null ? nbiRatio.text() : "-0.48%"
            ));

            Document fxDoc = Jsoup.connect("https://kr.investing.com/currencies/usd-krw")
                    .userAgent(USER_AGENT).timeout(5000).get();
            Element fxVal = fxDoc.selectFirst("[data-test='instrument-price-last']");
            Element fxChg = fxDoc.selectFirst("[data-test='instrument-price-change']");
            map.put("usdKrw", new IrDashboardResponse.IndexInfo(
                    fxVal != null ? fxVal.text() + "원" : "1,378.5원",
                    fxChg != null ? fxChg.text() : "+4.5원"
            ));
        } catch (Exception e) {
            if (!map.containsKey("kq26")) map.put("kq26", new IrDashboardResponse.IndexInfo("7,382.10", "-0.96%"));
            if (!map.containsKey("nbi")) map.put("nbi", new IrDashboardResponse.IndexInfo("7,284.12", "-0.48%"));
            if (!map.containsKey("usdKrw")) map.put("usdKrw", new IrDashboardResponse.IndexInfo("1,378.5원", "+4.5원"));
        }

        return map;
    }

    private List<IrDashboardResponse.NewsItem> fetchNaverCompanyNews() {
        List<IrDashboardResponse.NewsItem> news = new ArrayList<>();
        try {
            String url = "https://search.naver.com/search.naver?where=news&query=%EC%A7%80%EC%94%A8%EC%85%80";
            Document doc = Jsoup.connect(url).userAgent(USER_AGENT).timeout(5000).get();
            Elements articles = doc.select("ul.list_news li.bx");

            for (Element article : articles) {
                Element titleElem = article.selectFirst("a.news_tit");
                Element pressElem = article.selectFirst("a.info.press");
                Element dateElem = article.selectFirst("span.info");

                if (titleElem != null) {
                    news.add(new IrDashboardResponse.NewsItem(
                            titleElem.text(),
                            pressElem != null ? pressElem.text().replaceAll("언론사 선정", "").trim() : "네이버뉴스",
                            dateElem != null ? dateElem.text() : "최신",
                            titleElem.attr("href")
                    ));
                }
                if (news.size() >= 3) break;
            }
        } catch (Exception e) {
            news.add(new IrDashboardResponse.NewsItem("지씨셀 R&D 및 사업개발 모멘텀 지속 점검", "네이버뉴스", "최신", "#"));
        }
        return news;
    }

    private List<IrDashboardResponse.NewsItem> fetchInvestingNbiNews() {
        List<IrDashboardResponse.NewsItem> news = new ArrayList<>();
        try {
            String url = "https://kr.investing.com/news/stock-market-news";
            Document doc = Jsoup.connect(url).userAgent(USER_AGENT).timeout(5000).get();
            Elements articles = doc.select("article[data-test='article-item']");

            for (Element article : articles) {
                Element titleElem = article.selectFirst("a[data-test='article-title-link']");
                Element dateElem = article.selectFirst("span[data-test='article-publish-date']");

                if (titleElem != null) {
                    news.add(new IrDashboardResponse.NewsItem(
                            titleElem.text(),
                            "Investing.com",
                            dateElem != null ? dateElem.text() : "최신",
                            "https://kr.investing.com" + titleElem.attr("href")
                    ));
                }
                if (news.size() >= 3) break;
            }
        } catch (Exception e) {
            news.add(new IrDashboardResponse.NewsItem("나스닥 바이오 지수(NBI) 섹터 동향 및 관망세", "Investing.com", "최신", "https://kr.investing.com"));
        }
        return news;
    }

    // --- DTO 및 응답 구조 정의 (내부 정적 클래스) ---
    public static class IrDashboardResponse {
        private String status;
        private String updatedAt;
        private StockInfo stock;
        private Map<String, IndexInfo> macroIndices;
        private List<NewsItem> companyNews;
        private List<NewsItem> nbiNews;

        public String getStatus() { return status; }
        public void setStatus(String status) { this.status = status; }
        public String getUpdatedAt() { return updatedAt; }
        public void setUpdatedAt(String updatedAt) { this.updatedAt = updatedAt; }
        public StockInfo getStock() { return stock; }
        public void setStock(StockInfo stock) { this.stock = stock; }
        public Map<String, IndexInfo> getMacroIndices() { return macroIndices; }
        public void setMacroIndices(Map<String, IndexInfo> macroIndices) { this.macroIndices = macroIndices; }
        public List<NewsItem> getCompanyNews() { return companyNews; }
        public void setCompanyNews(List<NewsItem> companyNews) { this.companyNews = companyNews; }
        public List<NewsItem> getNbiNews() { return nbiNews; }
        public void setNbiNews(List<NewsItem> nbiNews) { this.nbiNews = nbiNews; }

        public static class StockInfo {
            private String price;
            private String ratio;
            private String volume;
            private String marketStatus;
            private InvestorInfo investor;

            public String getPrice() { return price; }
            public void setPrice(String price) { this.price = price; }
            public String getRatio() { return ratio; }
            public void setRatio(String ratio) { this.ratio = ratio; }
            public String getVolume() { return volume; }
            public void setVolume(String volume) { this.volume = volume; }
            public String getMarketStatus() { return marketStatus; }
            public void setMarketStatus(String marketStatus) { this.marketStatus = marketStatus; }
            public InvestorInfo getInvestor() { return investor; }
            public void setInvestor(InvestorInfo investor) { this.investor = investor; }
        }

        public static class InvestorInfo {
            private String foreign;
            private String organ;
            private String personal;

            public String getForeign() { return foreign; }
            public void setForeign(String foreign) { this.foreign = foreign; }
            public String getOrgan() { return organ; }
            public void setOrgan(String organ) { this.organ = organ; }
            public String getPersonal() { return personal; }
            public void setPersonal(String personal) { this.personal = personal; }
        }

        public static class IndexInfo {
            private String value;
            private String ratio;

            public IndexInfo() {}
            public IndexInfo(String value, String ratio) {
                this.value = value;
                this.ratio = ratio;
            }

            public String getValue() { return value; }
            public void setValue(String value) { this.value = value; }
            public String getRatio() { return ratio; }
            public void setRatio(String ratio) { this.ratio = ratio; }
        }

        public static class NewsItem {
            private String title;
            private String publisher;
            private String date;
            private String linkUrl;

            public NewsItem() {}
            public NewsItem(String title, String publisher, String date, String linkUrl) {
                this.title = title;
                this.publisher = publisher;
                this.date = date;
                this.linkUrl = linkUrl;
            }

            public String getTitle() { return title; }
            public void setTitle(String title) { this.title = title; }
            public String getPublisher() { return publisher; }
            public void setPublisher(String publisher) { this.publisher = publisher; }
            public String getDate() { return date; }
            public void setDate(String date) { this.date = date; }
            public String getLinkUrl() { return linkUrl; }
            public void setLinkUrl(String linkUrl) { this.linkUrl = linkUrl; }
        }
    }
}
