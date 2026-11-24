# **Zap2xml\_Dispatcharr\_Plugin**



## **A plugin for** [**Dispatcharr**](https://github.com/dispatcharr/dispatcharr) **to automatically add EPG Data**

### 

### **Installation**



Download the Zip file and either place in the Dispatcharr Plugins folder or go to the Plugins page in Dispatcharr and Import Plugin.

Download channels.db and update in your Dispatcharr /data/plugins/zap2xml folder when available for updated channels



### **Fork**

This fork adds lineup channel numbers to Gracenote station display names and serializes downloads across
Dispatcharr worker processes. Install updates from the plugin repository:

`https://raw.githubusercontent.com/tylertufano/zap2xml_dispatcharr_plugin/tufanito/manifest.json`



### **Usage**



**Lineup IDs:** Add one or multiple lineupId's (comma separated)

**Total Hours to Fetch:** Adjust in 6 hour increments Minimum 6 Maximum 360 (Note 348 is safer as the data is not always available for 360 hours)

**Delay Between Requests (sec):** The seconds between each 6 hour request.  5 is default but you can lower just want to avoid rate limiting on the website.

**HTTP User Agent:** By default it rotates between different desktop browser agents so should not need override

**Refresh Interval (hours):** Default 24 hours will automatically rerun.

**EPG Name:** Name saved to the Dispatcharr /data/epgs folder.  Default is Zap2xml and if multiple lineups will save as \_merged

#### Artwork preferences

- **Station Logo Style** offers Provider Default, Dark, White, Gray, and Light. This sets the station icon in the XMLTV guide. Dispatcharr's EPG logo controls handle assigning those icons to channels.
- **Include Programme Artwork** is on by default. Turn it off to omit programme icons.
- **Programme Artwork Style** offers Provider Default, Landscape Iconic (art without title text), and Landscape Banner with Title. Alternative artwork is checked only for stations already mapped to the matching Zap2XML EPG source, including the merged source. New sources use supplied artwork until stations are mapped and the guide refreshes again.

Saved settings take effect on the next manual or scheduled Zap2XML download. Provider Default uses the supplied icon URLs as-is. An alternative is used only after its image type and signature are verified. Missing, unavailable, or unrecognized variants fall back to the supplied icon.

Each refresh allows up to 128 artwork requests and 25 seconds of cumulative request time across all lineups, with up to 2 seconds per request. A request that exceeds its wall-clock limit or receives HTTP 429 stops further requests in that refresh. The persistent cache holds up to 8,192 results, with verified images cached for 30 days and confirmed misses for two days. The refresh log summarizes selections, fallbacks, cache hits, and assets skipped when the request budget runs out.



### **FAQ's**



**Can you add country name not on** [**Valid Country Codes**](https://github.com/jesmannstl/zap2xml_dispatcharr_plugin/blob/main/Valid%20Country%20Codes.md) **page?**
No.  The original zap2xml only supported US and Canada but I was able to expand it for most supported countries in North and South America plus the Caribbean.  This method did not work for Europe or Australia.  The data provider itself does not support Asia, Africa or Antarctica.



**Where can I find a lineupId for this area?**
I created a lineup lookup for supported countries at [https://zap2xml.jesmann.com](https://zap2xml.jesmann.com) just select the country name and add the Zip/Postal Code and search.

