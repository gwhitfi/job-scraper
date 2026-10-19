

var addthis_config = {
          services_compact: 'email, facebook, twitter, more',
          services_exclude: 'print'
}

var VacanciesAreOnThisPage = true

//window.onerror = function nada(){}

function centerWin(zwidth,zheight){
       var winHeight
       var winWidth
       winHeight = screen.availHeight
       winWidth = screen.width
       if (zwidth > winWidth){zwidth=winWidth}
       if (zheight > winHeight){zheight=winHeight}
       zleft = parseInt((winWidth/2)-(zwidth/2))
       if (zleft < 10){zleft = 0}
       ztop = parseInt((winHeight/2)-(zheight/2))
       if (ztop < 25){ztop = 0}
       var x = "width=" + zwidth + ",height=" + zheight + ",left=" + zleft + ",top=" + ztop
       return x
}

function applyFor(posJobCode,posFirstChoice,posSpecialty){
       var openwindow = false
       if (document.layers){
              alert("Please go back and then click 'Start an Application' to apply for this position.")
              return false;
       }
       var OpenWin = true
       try {
              if (typeof self.opener.updateApp == "boolean"){
                     OpenWin = false
              }
       } catch (e) {
              OpenWin = true
       }

       if (OpenWin){
              var applyFor = window.open("https://www.applitrack.com/acsd3/onlineapp/_application.aspx?posJobCodes=" + posJobCode + "&posFirstChoice=" + posFirstChoice + "&posSpecialty=" + posSpecialty + "","applyFor","location=no,resizable=yes,scrollbars=yes," + centerWin(795,550))
              applyFor.focus()
       } else {
              window.opener.location = "javascript: applyFor('" + posJobCode + "','" + posFirstChoice + "','" + posSpecialty + "')"
              self.close()
       }
}

function oldWay(){
       var openwindow = false
       if (document.layers){
              alert("Please go back and then click 'Start an Application' to apply for this position.")
              return false;
       }
       if (opener == null){
              var applyFor = window.open("https://www.applitrack.com/acsd3/onlineapp/_application.aspx?posJobCodes=" + posJobCode + "&posFirstChoice=" + posFirstChoice + "&posSpecialty=" + posSpecialty + "","applyFor","location=no,resizable=yes,scrollbars=yes," + centerWin(795,550))
              applyFor.focus()
       }else{
              if (typeof self.opener == "undefined"){
                     var applyFor = window.open("https://www.applitrack.com/acsd3/onlineapp/_application.aspx?posJobCodes=" + posJobCode + "&posFirstChoice=" + posFirstChoice + "&posSpecialty=" + posSpecialty + "","applyFor","location=no,resizable=yes,scrollbars=yes," + centerWin(795,550))
                     applyFor.focus()
              }else{
                     if (typeof self.opener.updateApp == "boolean"){
                           window.opener.location = "javascript: applyFor('" + posJobCode + "','" + posFirstChoice + "','" + posSpecialty + "')"
                           self.close()
                     }else{
                           var applyFor = window.open("https://www.applitrack.com/acsd3/onlineapp/_application.aspx?posJobCodes=" + posJobCode + "&posFirstChoice=" + posFirstChoice + "&posSpecialty=" + posSpecialty + "","applyFor","location=no,resizable=yes,scrollbars=yes," + centerWin(795,550))
                           applyFor.focus()
                     }
              }
       }
}
function applyForInternal(posJobCode,posFirstChoice,posSpecialty){
    var passDistrictCode;

       var applyFor = window.open("https://www.applitrack.com/acsd3/onlineapp/_application.aspx?isInternal=1&posJobCodes=" + posJobCode + "&posFirstChoice=" + posFirstChoice + "&posSpecialty=" + posSpecialty + passDistrictCode + "","applyFor","location=no,resizable=yes,scrollbars=yes," + centerWin(795,550))
       applyFor.focus()
}

function applyForOutsideDistrict(district,posJobCode,posFirstChoice,posSpecialty){
    var applyPage;
    var applyWinSize;
    
        applyPage = '_application.aspx';
    
       var applyFor = window.open("https://www.applitrack.com/" + district + "/onlineapp/" + applyPage + "?posJobCodes=" + posJobCode + "&posFirstChoice=" + posFirstChoice + "&posSpecialty=" + posSpecialty + "","applyFor","location=no,resizable=yes,scrollbars=yes," + centerWin(795,550))
       applyFor.focus()
}

function toggle_block(i) {
       if (AppliTrack_$(i).style.display == 'none'){ AppliTrack_$(i).style.display = 'block';} else {AppliTrack_$(i).style.display = 'none';}
       setUpPageBreaks()
       return false;
}

function submitAppliTrackSearch(){
       AppliTrack_SearchExecute(null);
}

/* Deprecated; use AppliTrack_SearchExecute() */
function AppliTrackSort(key){
       winLoc = window.location.toString()
       if (window.location.search == "") {
              window.location = winLoc + "?AppliTrackSort=" + escape(key)
       }else{
              if (window.location.search.indexOf("AppliTrackSort=") > 0){
                     var currLoc = winLoc.substring(0,winLoc.indexOf("AppliTrackSort="))
                     window.location = winLoc.replace(/([^a-zA-Z0-9\-\.]+AppliTrackSort=)([a-zA-Z0-9\-\.]+)([&a-zA-Z0-9\-\.]*)/i,'$1' + escape(key) + '$3');
              }else{
                     window.location = winLoc + (winLoc.indexOf("?") < 0?"?":"&") + "AppliTrackSort=" + escape(key)
              }
       }
}

function AppliTrackSearchFocus(){
    var e = AppliTrack_$('AppliTrackPostingSearchBasic');
       if (e && e.value == "Search Postings"){
              e.value = ""
              e.style.color = "black"
              e.style.fontStyle = "normal"
       }
}
function AppliTrackSearchBlur(){
    var e = AppliTrack_$('AppliTrackPostingSearchBasic');
       if (e && e.value == ""){
              e.value = "Search Postings"
              e.style.color = "#999999"
              e.style.fontStyle = "italic"
       }
}
function AppliTrack_WatchForEnter(e){
       if(window.event)
        key = window.event.keyCode;     //IE
    else
        key = e.which;     //firefox

    if (key == 13)
    {
              AppliTrack_SearchExecute(null);

              if(window.event){
                  window.event.cancelBubble = true;
                  window.event.returnValue = false;
              }

              if (event && event.stopPropagation) {
            event.stopPropagation();
        }
        if (event && event.preventDefault) {
            event.preventDefault();
        }
        return false;
    }
}

function AppliTrackInit(){
       try{
           var sf = AppliTrack_$('AppliTrackPostingSearchBasic');
           var sc = AppliTrack_$('AppliTrackSearchSubCategory');
           if(sf){sf.value = "Search Postings"}
           if(sc){sc.disabled = true;}
           if (window.location.search.indexOf("category") > 0 || window.location.search.indexOf("AppliTrackPostingSearchBasic") > 0){
                  AppliTrack_$('AppliTrackPostings').scrollIntoView(true)
           }
       }
    catch(e){}
}
setTimeout('AppliTrackInit()',100)
function AppliTrack_ToggleAdvancedSearch(){
    var hc = AppliTrack_$('AppliTrackSearchAdvancedHeaderCollapsed');
    var he=AppliTrack_$('AppliTrackSearchAdvancedHeaderExpanded');
    var c=AppliTrack_$('AppliTrackSearchAdvancedContainer');
    var s=AppliTrack_$('AppliTrackSearchSimpleContainer');
    if(!hc||!he||!c) return;
    if(hc.style.display=='block'){
        hc.style.display='none';
        he.style.display='block';
        c.style.display='block';
        s.style.opacity='.5';
        s.style.filter='alpha(opacity=50)';
    }
    else{
        hc.style.display='block';
        he.style.display='none';
        c.style.display='none';
        s.style.opacity='1';
        s.style.filter='alpha(opacity=100)';
    }
}

function AppliTrack_onKeyPressToggle(e){
    if(window.event) {
        key = window.event.keyCode;     //IE
    } else {
        key = e.which;     //firefox
    }
    //toggle advanced settings on 'enter' and 'space' keypress
    if (key == 13 || key == 32) {
        AppliTrack_ToggleAdvancedSearch();
    }
}

function AppliTrack_SearchCategorySelect(){
    var c = AppliTrack_$('AppliTrackSearchCategory');
    var sc=AppliTrack_$('AppliTrackSearchSubCategory');
    if(!c||!sc){return;}
    if(c.options&&c.selectedIndex > 0){
        sc.disabled=false;
        var v = c.options[c.selectedIndex].value;
        if(v!=''){
            try{
                eval('var g = '+v);
                sc.options.length = 0;
                sc.options[sc.options.length] = new Option('All ' + g.id + ' postings','');

                for(var i=0;i < g.vals.length;i++){
                    if(g.vals[i]==''){
                        sc.options[sc.options.length] = new Option('All ' + g.id + ' postings','');
                    }
                    else{
                        sc.options[sc.options.length] = new Option(g.vals[i],g.vals[i]);
                    }
                }
            }catch(e){/*malformed option*/}
        }
    }
    else{
        sc.options.length = 0;
        sc.disabled = true;
    }

}

function AppliTrack_RadiusSelect(){
    var l = AppliTrack_$('AppliTrackSearchLocation');
    var sr=AppliTrack_$('AppliTrackSearchRadius');
    var zct=AppliTrack_$('AppliTrackZipCode');
    var zcv = AppliTrack_$('AppliTrackZipValidator');
     if(!l||!sr){return;}
      if(l.options[l.selectedIndex].value != ''){
        sr.disabled = true;
        zct.disabled = true;
        zcv.style.visibility='hidden';
        }
        else
        {
          sr.disabled = false;
          zct.disabled = false;
        }
}


function AppliTrack_SearchExecute(f /*,execute search, default advanced form visible*/){

  var ll = AppliTrack_$('AppliTrackSearchLocation');
  var zct = AppliTrack_$('AppliTrackZipCode');
  var zcv = AppliTrack_$('AppliTrackZipValidator');
  var sbb = AppliTrack_$('AppliTrackPostingSearchBasic');
  var validateZip =true;
  if (zct !=null) {
    if (zct.value != '' ){
        validateZip =  /(^\d{5}$)|(^\d{5}-\d{4}$)/.test(zct.value);
    }
    else
    {
        validateZip = true;
    }
  }
  else
  {
    validateZip = true;
  }

  if(( sbb.value !='Search Postings') || validateZip ){
    var loc = window.location.search;
       if(loc.substring(0,1) == '?'){
        //always replace the current search - refactor this to eliminate compounding searching
           loc = 'all=1';
        //loc= loc.substring(1,loc.length);
       }
       if(f&&(f.tagName+'').toLowerCase()=='form')
       { f.onSubmit = null; }

       if(!arguments||arguments.length<2||arguments[1]){
           //search
        var k = AppliTrack_$('AppliTrackPostingSearchBasic');
        var ak= AppliTrack_$('AppliTrackSearchKeyword');
        var tt= AppliTrack_$('AppliTrackSearchTitle');
        var d = AppliTrack_$('AppliTrackSearchDistrict');
        var l = AppliTrack_$('AppliTrackSearchLocation');
        var c = AppliTrack_$('AppliTrackSearchCategory');
        var sc= AppliTrack_$('AppliTrackSearchSubCategory');
        var pd= AppliTrack_$('AppliTrackSearchPostDateRange');
        var sregion = AppliTrack_$('AppliTrackSearchRegion');
        var zc =AppliTrack_$('AppliTrackZipCode');
        var zipr = AppliTrack_$('AppliTrackSearchRadius');
        var int = AppliTrack_$('internal');
        var state= AppliTrack_$('AppliTrackSearchState');

           var s = '';
           if(!AppliTrack_isEmpty(k)&&'Search Postings'!=''+k.value)
             {
               s+=(s!=''?' ':'');
               if(k.value !=''){ s+='title:' + k.value;}
             }
           if(!AppliTrack_isEmpty(ak)){s+=(s!=''?' ':'') + ak.value;}
           if(d&&d.options&&d.selectedIndex>=0&&''!=''+d.options[d.selectedIndex].value){
               for(var i=1;i < d.options.length;i++){
                   if(d.options[i].selected && d.options[i].value+''!=''){
                       s+=(s!=''?' ':'') + 'district:' + d.options[i].value;
                   }
               }
               loc = AppliTrack_RemoveParam(loc,'district');
           }
        var radius='';
        if (zipr != null)
        {
            if (zipr.value !='')
            {
            radius=' radius%3A"'+ zipr.value +'"';
            }
        }
        if (l != null)
        {
               if(l&&l.options&&''!=''+l.options[l.selectedIndex].value){
                   s+=(s!=''?' ':'') + 'location:"' + l.options[l.selectedIndex].value + '"';
                   loc = AppliTrack_RemoveParam(loc,'location');
               }
        }
           if(tt&&''!=''+tt.value){
               s+=(s!=''?' ':'') + 'title:"' + tt.value + '"';
               loc = AppliTrack_RemoveParam(loc,'title');
           }
        if(pd&&pd.options&&''!=''+pd.options[pd.selectedIndex].value){
               s+=(s!=''?' ':'') + 'dateposted:' + pd.options[pd.selectedIndex].value + '';
               loc = AppliTrack_RemoveParam(loc,'dateposted');
           }
           if(c&&c.options&&''!=''+c.options[c.selectedIndex].value){
            try{
                eval('var g = '+c.options[c.selectedIndex].value);
                s+=(s!=''?' ':'') + 'category:"' + g.id + '"';
                loc = AppliTrack_RemoveParam(loc,'category');
            }catch(e){/*malformed option*/}
           }

           if(sregion && sregion.options && sregion.options[sregion.selectedIndex] != null && ''!=''+sregion.options[sregion.selectedIndex].text){
                try{
                    s+=(s!=''?' ':'') + 'region:"' + sregion.options[sregion.selectedIndex].text + '"';
                    loc = AppliTrack_RemoveParam(loc,'region');
                }catch(e){/*malformed option*/}
           }

           if(sc&&sc.options&&!sc.disabled&&sc.selectedIndex>=0&&'' != ''+sc.options[sc.selectedIndex].value){
               s+=(s!=''?' ':'') + 'subcategory:"' + sc.options[sc.selectedIndex].value + '"';
               loc = AppliTrack_RemoveParam(loc,'subcategory');
           }

           if (typeof CustomSearchFormExtender == 'function') {
               s = s + CustomSearchFormExtender();
           }
           loc = AppliTrack_AddParam(loc,'AppliTrackPostingSearch',s);
       }
    if (zc != null){
        loc = AppliTrack_AddParam(loc,'AppliTrackZipCode', zc.value);
        loc = AppliTrack_AddParam(loc,'AppliTrackZipRadius', zipr.value);
    }
    if (state != null){
        loc = AppliTrack_AddParam(loc,'AppliTrackSearchState', state.value);
    }
    if (int != null)
    {
        loc = AppliTrack_AddParam(loc,'internal', int.value);
    }
       //sort
    var so = AppliTrack_$('AppliTrackSort');
       if(so&&so.options&&''!=''+so.options[so.selectedIndex].value){
           loc = AppliTrack_AddParam(loc ,'AppliTrackSort',''+so.options[so.selectedIndex].value);
       }

       //display
    var m = AppliTrack_$('AppliTrackLayoutMode');
       if(m&&m.options&&''!=''+m.options[m.selectedIndex].value){
           loc = AppliTrack_AddParam(loc ,'AppliTrackLayoutMode',''+m.options[m.selectedIndex].value);
       }

    //expand/collapse
    if(arguments&&arguments.length==3&&arguments[2]&&(navigator.vendor+'').indexOf('Apple')==-1){
        loc = AppliTrack_AddParam(loc,'AppliTrackSearch','expanded');
    }



    //GET
    if(loc.match(/^&/g))  {loc = loc.substring(1,loc.length);}
    if(!loc.match(/^\?/g)){loc = '?' + loc;}
    if(loc.match(/[&\?]$/)!=null){loc = loc.substr(0,loc.length-1);}
        window.location = window.location.toString().replace(window.location.search,'') + loc;

    }

    // if no zip code show this
    else
    {
        if (zcv != null){
            zcv.style.visibility="visible";
        }
    }
}


function AppliTrack_isEmpty(s){
    var st = (s&&s.value?s.value:s);
    return !(st && ''!=''+st && 'undefined'!=''+st);
}

function AppliTrack_AddParam(loc,key,val){
    var rv = loc+'';
    if(!AppliTrack_isEmpty(key)&&!AppliTrack_isEmpty(val)){
        if (rv.match(new RegExp(key+'=',"ig"))){
            rv = AppliTrack_RemoveParam(rv,key);
           }
        if(rv.length>0 && !rv.match('&$')){rv += '&';}
           rv += key + "=" + escape(val);

    }
    return rv;
}

function AppliTrack_RemoveParam(loc,key){

    var rv = (loc+'').split('&');
    if(key!= ''){
        for(var i=0;i < rv.length;i++){
           if(rv[i].substring(0,key.length) == key){
                rv[i]='';
            }
        }
    }
    return (rv?rv.join('&').replace('&&','&'):'');
}
function AppliTrack_$(id) {
    var o = null;
    if( document.layers ) {
        o = document.layers[id];
    } else if( document.all ) {
        o = document.all[id];
    } else if( document.getElementById ) {
        o = document.getElementById(id);
    }
    return o;
}


function updateEmailLinkFromCurrentWindowLocation(mailSubject, mailMsg, queryStrings)
{
    queryStrings = queryStrings.replace('default.aspx', 'JobPostings/view.asp');
    var newLine = "%0D%0A";
    var sanitizedUrl = sanitizeUrl(window.location.href, queryStrings)
    window.location.href = mailSubject + mailMsg + newLine + newLine + encodeURIComponent(sanitizedUrl);
}

function updateHrefFromCurrentWindowLocation(queryStrings, isOpenInNewTab, isClearPreviousParams, isReplaceDefault){
    var currentLocation = window.location.href;

    if(isClearPreviousParams) currentLocation =  window.location.origin + window.location.pathname;

    if(isReplaceDefault) currentLocation = currentLocation.replace('default.aspx', 'JobPostings/view.asp');

    if (currentLocation.indexOf('?') == -1 && queryStrings.indexOf('?') == -1) {
        currentLocation = currentLocation + "?";
    }
    else {
        if (currentLocation.indexOf('?') != -1) currentLocation = currentLocation + '&';
    }

    if(isOpenInNewTab)
        window.open(sanitizeUrl(currentLocation, queryStrings), '_blank' );
    else
        window.location.href = sanitizeUrl(currentLocation, queryStrings);
}

function sanitizeUrl(currentLocation, queryStrings){
    queryStrings = queryStrings.replace("ApplitrackHardcodedURL?1=1", "");
    queryStrings = queryStrings.replace("ApplitrackHardcodedURL", "");

    return removeDuplicateQueryStringsFromUrl(currentLocation + queryStrings);
}

function removeDuplicateQueryStringsFromUrl(currentLocation)
{
	// Example : "http://qa4.applitrack.com/dex/jobhomepage.html?AppliTrackSort=newest&AppliTrackSort=newest"
	var delimeter = '&';
	var newUrl = [];
	var urlArray = currentLocation.split(delimeter);

	urlArray.forEach(function(urlParameter, index)
	{
		if (newUrl.indexOf(urlParameter) == -1 && urlParameter.indexOf('?') != 0)
		{
			if((urlParameter.indexOf('/') > 0 && index == 0) || urlParameter.indexOf('=') > 0)
			{
				newUrl.push(urlParameter);
			}
		}
	});

	return newUrl.join(delimeter);
}

document.write('<div id=\'AppliTrackOutput\'><form id=\'AppliTrackSearchForm\' name=\'AppliTrackSearchForm\' onsubmit=\'return false;\'><a name=AppliTrackPostings id=AppliTrackPostings></a><link rel=\'stylesheet\' type=\'text/css\' href=\'/olacommon/jobpostings/postingStyles.v3.css?v=8-17-2016\' /><div class=\'AppliTrackListHeader\'id=\'ApplicantMain\'><div>Openings as of 10/18/2026</div></div><br><div style=\'display: none;\'>force display?True</div><div id=\'AppliTrackSearchContainer\'><table width=\'100%\' class=\'AppliTrackSearchBG\'><tr><td class="normal" width="100%">Viewing All Types&nbsp;(<b>3</b> openings)</td><td align=right nowrap><div id=\'AppliTrackSearchSimpleContainer\'><label for=\'AppliTrackPostingSearchBasic\' class=\'HiddenButReadable\'>Search Postings:</label><input type=\'text\' size=\'15\' id=\'AppliTrackPostingSearchBasic\' style=\'font-style: italic; color: #999999\' onfocus=\'AppliTrackSearchFocus()\' onblur=\'AppliTrackSearchBlur()\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\' /><input type=\'button\' value=\'Go\'&nbsp; class=\'button\' style=\'padding:0px 0px;\' onclick=\'submitAppliTrackSearch()\' /></div></td></tr></table><div id=\'AppliTrackSearchAdvancedHeaderCollapsed\' style=\'display:block;\' onclick=\'AppliTrack_ToggleAdvancedSearch()\' tabindex=\'0\' aria-role=\'button\' onkeydown=\'AppliTrack_onKeyPressToggle(event)\'>Options</div><div id=\'AppliTrackSearchAdvancedContainer\' style=\'display:none;\'><div class=\'section\' style=\'padding-bottom:4px;border-bottom:solid 1px #ddd;\'><div class=\'header\'>Search Job Postings</div><div><label for=\'AppliTrackSearchKeyword\'><span class=\'fixedCol1 label\' nowrap=\'nowrap\'><u>Posting</u> contains:</span></label><input style=\'width:250px;\' type=\'text\' name=\'AppliTrackSearchKeyword\' id=\'AppliTrackSearchKeyword\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\'/></div><div><label for=\'AppliTrackSearchTitle\'><span class=\'fixedCol1 label\' nowrap=\'nowrap\'><u>Title</u> contains:</span></label><input style=\'width:250px;\' type=\'text\' name=\'AppliTrackSearchTitle\' id=\'AppliTrackSearchTitle\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\' /></div><div><span class=\'fixedCol1 label\'>In Category:</span><select name=\'AppliTrackSearchCategory\' id=\'AppliTrackSearchCategory\' onchange=\'AppliTrack_SearchCategorySelect()\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\'><option style=\'color:#666;font-style: italic;\'>Select Category</option><option value=\'{id:"Custodial Services",vals:["Custodian"]}\'>Custodial Services</option><option value=\'{id:"Nutritional Services",vals:["Food Service Operator"]}\'>Nutritional Services</option><option value=\'{id:"Transportation",vals:["Bus Driver"]}\'>Transportation</option></select><span class=\'label\'> and </span><span class=\'label\' style=\'white-space:nowrap\'>SubCategory:<select name=\'AppliTrackSearchSubCategory\' id=\'AppliTrackSearchSubCategory\' disabled=\'disabled\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\'><option></option></select></span></div><div><span class=\'fixedCol1 label\'>At Location:</span><select name=\'AppliTrackSearchLocation\' id=\'AppliTrackSearchLocation\' onchange=\'AppliTrack_RadiusSelect()\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\'><option value=\'\' style=\'font-style:italic;color:#999\'> -- Any Location -- </option><option value="Starr Elementary School">Starr Elementary School</option><option value="Starr-Iva Middle School">Starr-Iva Middle School</option><option value="Transportation Department">Transportation Department</option></select></div><div id="AppliTrackSearchZipCodeSection"><span class=\'fixedCol1 label\'>Within:</span><select name=\'AppliTrackSearchRadius\' id=\'AppliTrackSearchRadius\' onchange=\'AppliTrack_SearchCategorySelect()\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\'><option value=\'5\'>5</option><option value=\'10\'>10</option><option value=\'15\'>15</option><option value=\'25\'>25</option><option value=\'50\'>50</option><option value=\'100\'>100</option><option value=\'150\'>150</option></select><span class=\'fixedCol1 label\' style=\'margin-left: -25px;\'>miles of Zip:</span><label for=\'AppliTrackZipCode\' class=\'HiddenButReadable\'>Zip Code</label><input style=\'width:70px;\' type=\'text\' name=\'AppliTrackZipCode\' id=\'AppliTrackZipCode\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\' /><stronge name=\'AppliTrackZipValidator\' id=\'AppliTrackZipValidator\' style=\'visibility:hidden;text-align: left;color:red;\'>Invalid Zipcode</stronge><input style=\'width:250px;\' type=\'hidden\' name=\'internal\' id=\'internal\' value=\'\'&nbsp; /></div><button id="AppliTrackSearchButton" type="button" style=\'float:right;margin-right:10px;cursor:pointer;\' onclick=\'AppliTrack_SearchExecute(null)\'>Search</button><div><span class=\'label\' style=\'width:125px;text-align:right;\'>Posted in the last:</span><select name=\'AppliTrackSearchPostDateRange\' id=\'AppliTrackSearchPostDateRange\' onKeyPress=\'return AppliTrack_WatchForEnter(event)\'><option value=\'\'>All Dates</option><option value=\'7\'>7 Days</option><option value=\'14\'>14 Days</option><option value=\'30\'>30 Days</option></select></div></div><div class=\'section\' style=\'vertical-align: top;\'><span style=\'white-space:nowrap\'><span class=\'header\'>Sort Results By:</span><select name=\'AppliTrackSort\' id=\'AppliTrackSort\' class=\'data\'><option class=\'data\' value=\'type\' selected=\'selected\'>type</option><option class=\'data\' value=\'title\'>title</option><option class=\'data\' value=\'location\'>location</option><option class=\'data\' value=\'newest\'>newest</option><option class=\'data\' value=\'oldest\'>oldest</option></select></span><span style=\'white-space:nowrap\'><span class=\'header\'>Display as:</span><select name=\'AppliTrackLayoutMode\' id=\'AppliTrackLayoutMode\' class=\'data\'><option value=\'detail\' selected=\'selected\'>Detailed View</option><option value=\'condensed\'>Condensed View</option></select></span></div></div><div id=\'AppliTrackSearchAdvancedHeaderExpanded\' style=\'display:none;\' onclick=\'AppliTrack_ToggleAdvancedSearch()\'>Options</div></div><div id=\'AppliTrackListContent\'><p align=center class=noprint id=\'p293_h\'><span class=\'ListHeader\'>Openings as of 10/18/2026<br/></span></p><ul class=\'postingsList\' id=\'p293_\'><table class=\'title\' style=\'padding: 0px; width:99.2%\'><tr><td id=\'wrapword\' style=\'width: 950px; padding: 5px;\'>Custodian - Immediate Opening</td><td style=\'width: 275px;\'><span class=\'title2\' style=\'float:right;text-align:right;margin-top: 8px;margin-right: 6px;\'> JobID: 293 <input type=\'button\' value=\' Apply \' onclick="applyFor(\'293\',\'Custodial Services\',\'Custodian\')" class=\'screenOnly ApplyButton\' /></span></td></tr></table><div style=\'position:relative;\'><li><span class=\'label\'>Position Type:</span><br/>&nbsp;&nbsp;<span class=\'normal\'>Custodial Services/</span><span class=\'normal\'>Custodian</span><br/><br/></li><li><span class="label" >Date Posted:</span><br/>&nbsp;&nbsp;<span class="normal">10/2/2026</span><br/><br/></li><li><span class="label" >Location:</span><br/>&nbsp;&nbsp;<span class="normal">Starr-Iva Middle School</span><br/><br/></li><span>&nbsp&nbsp</span><span class="label" >Additional Information: &nbsp;</span><span class="normal"><a href="javascript:void(toggle_block(\'DescriptionText293_\'))"><u>Show/Hide</u></a></span><br/><span id=\'DescriptionText293_\' style=\'border: 1px solid black; padding:5px; display:none; opacity:.8;\'><span class="normal"><p><strong>Anderson School District 3 is seeking a dedicated and dependable individual to join our team as a Custodian at Starr-Iva Middle School. This is an <u>immediate opening</u> and a great opportunity to be part of a school community that takes pride in providing a clean, safe, and welcoming environment for students and staff.</strong></p><p>&nbsp;</p><p><strong>General Description:</strong><br>The purpose of this classification is to maintain and tend to the facilities of the school to provide a safe and pleasant atmosphere for students, teachers, staff, and visitors. Responsible for sweeping, mopping, buffering, and waxing floors, washing windows, cleaning restrooms, changing filters and lights, performing basic facility repair and maintenance, and moving and maintaining equipment.&nbsp;<br>&nbsp; &nbsp;&nbsp;<br><strong>Essential Duties:</strong><br>- Clean, maintain, and tend to the facilities of the assigned campus, including sweeping, mopping, buffering, and waxing floors, vacuuming carpeted areas, washing windows, cleaning restrooms, changing filters and lights, etc.<br>- Assist in cleaning and maintaining outdoor areas.<br>- Resupply toilet paper, soap, paper towels, and other supplies in restrooms.<br>- Transport equipment and furniture; set up chairs and tables as needed for events.<br>- Clean and sanitize classrooms, restrooms, and cafeteria areas.<br>- Remove and dispose of trash and recycling regularly.<br>- Maintain and check fire extinguishers regularly.<br>- Manage and complete work orders.<br>- Ensure all supplies are stored properly and that storage areas are clean and organized.<br>- Maintain and repair related equipment as needed.<br>- Assist other custodians and personnel as needed.<br>- Be available for emergency or unplanned events that may transpire during the course of a school year.<br>- Participate in staff development programs.<br>- Performs other duties as directed.</p><p>&nbsp;</p><p><strong>Knowledge, Skills and Abilities:</strong><br>Knowledge of custodial equipment and cleaning supplies, installation, and general janitorial procedures; Knowledge of school grounds, rules, regulations, and procedures; Ability to communicate and relate effectively to others orally and in writing; Ability to conduct precise measurements; Good interpersonal and time management skills. Ability to follow work orders and work with minimum supervision; Knowledge of proper safety procedures and protocols; proper lifting.<br>tools and equipment usage.</p><p>&nbsp;</p><p><strong>Tools and Equipment Usage:</strong><br>Use computers for data entry; or use hand-held power equipment or light and heavy machinery. Use computers for word processing, spreadsheets, or custom applications; Interpret policy and establish methods and procedures for testing, operating, or repairing machinery; Use mops, brooms, buffers, power tools, ladders, and other custodial and maintenance equipment; Manage inventory, property, or loss control.<br>&nbsp; &nbsp;&nbsp;<br><strong>Qualifications:</strong><br>- Completion of a High School Diploma or GED, preferred<br>- No Experience Required<br>- Ability to perform duties with awareness of all district, state, and federal requirements, as well as A3 Board Policies<br>&nbsp; &nbsp;&nbsp;<br><strong>Additional Information:</strong></p><p>&nbsp;</p><p><strong>Physical Requirements:</strong><br>Standing 1/3 to 2/3 of the time; Walking 1/3 to 2/3 of the time; Sitting 1/3 to 2/3 of the time; Gripping or feeling with hands over 2/3 of the time; Reaching with hands and arms over 2/3 of the time; Climbing or balancing under 1/3 of the time; Stooping, kneeling, crouching, or crawling under 1/3 of the time; &nbsp;Talking or hearing under 1/3 of the time; Tasting or smelling none of the time. Lifting - Up to 10lbs 1/3 to 2/3 of the time; Up to 25lbs 1/3 to 2/3 of the time; Up to 50lbs under 1/3 of the time; Up to 100lbs under 1/3 of the time; More than 100lbs none of the time.</p><p>&nbsp;</p><p><strong>Working Conditions and Hazards:</strong><br>Indoor Environment, Outdoor Environment, Noise, Extreme temperatures; Vibration; Moisture and/or humidity; Dust; Fumes; Gases; Poor Ventilation; Electrical hazards; Mechanical hazards; Chemical hazards; Bodily fluids and pathogens.<br>&nbsp; &nbsp;&nbsp;<br><strong>REPORTS TO:</strong> Principal&nbsp;with oversight from Head Custodian<br>&nbsp; &nbsp;&nbsp;<br><strong>TERMS OF EMPLOYMENT:</strong> 250 days.&nbsp;<br>&nbsp; &nbsp;&nbsp;<br><strong>JOB TYPE:</strong> Full-time<br>&nbsp; &nbsp;&nbsp;<br><strong>SALARY:</strong> Custodial Salary Schedule (Posted Online)&nbsp;<br>&nbsp; &nbsp;&nbsp;<br><strong>EVALUATION:</strong> Performance of this job will be evaluated annually by the principal.&nbsp;<br><br><strong>FLSA STATUS:</strong> Non-Exempt<br><br><strong>Benefits:&nbsp;</strong><br>Eligible employees may enroll in health insurance, which includes prescription coverage and wellness benefits. Other available insurance benefits include dental, vision, term life insurance, long-term disability, and flexible spending accounts for health and child care expenses.<br><br>State employees are also offered retirement plan options, including defined benefits and defined contribution plans. Additionally, eligible employees may elect to participate in the South Carolina Deferred Compensation Program, which is a voluntary, supplemental retirement savings plan offering 401(k) and 457 plan options.<br><br>State employees may also be eligible for other benefits, including tuition assistance; holiday, annual and sick leave; and discounts on purchases, travel, and more.<br><br><br>&nbsp;</p><p>&nbsp;</p><p>&nbsp;&nbsp;</p></span><br /><img src=\'https://www.applitrack.com/clear.gif\' height=8><br /></span><br/><div class=\'label\' align=\'right\' style=\'position:absolute; top:0%;right:1%;\'><span class=\'s\'><div class="addthis_toolbox addthis_default_style "><a class="addthis_button_preferred_1"></a><a class="addthis_button_preferred_2"></a><a class="addthis_button_preferred_3"></a><a class="addthis_button_compact"></a><a class="addthis_counter addthis_bubble_style"></a></div><script type="text/javascript" src="https://s7.addthis.com/js/250/addthis_widget.js#pubid=ra-4dfeb954767d3dc8"></script><br><a href=\'javascript:updateEmailLinkFromCurrentWindowLocation("mailto:?subject=Job Posting&body=","I thought you would be interested in an employment opportunity I found at Anderson School District 3. The position is Custodian - Immediate Opening. Please click the link below for more information about this vacancy.","ApplitrackHardcodedURL?1=1&AppliTrackJobId=293&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1")\'><span style=\'color:#4c4c4c;font-size:.9em;font-weight:normal;\'>Email To A Friend</span></a><br/>');
document.write('<a href=\'javascript:updateHrefFromCurrentWindowLocation("ApplitrackHardcodedURL?1=1&AppliTrackJobId=293&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1" , true, false, true)\'><span style=\'color:#4c4c4c;font-size:.9em;font-weight:normal;\'>Print Version</span> </a><br/></span></div><br/></div></ul><div style=\'width: 100%; height: .75px; background: gray; overflow: hidden; margin-bottom:4px;\'></div><br /><p align=center class=noprint id=\'p294_h\'><span class=\'ListHeader\'>Openings as of 10/18/2026<br/></span></p><ul class=\'postingsList\' id=\'p294_\'><table class=\'title\' style=\'padding: 0px; width:99.2%\'><tr><td id=\'wrapword\' style=\'width: 950px; padding: 5px;\'>Nutritional Services Operator - Immediate Opening</td><td style=\'width: 275px;\'><span class=\'title2\' style=\'float:right;text-align:right;margin-top: 8px;margin-right: 6px;\'> JobID: 294 <input type=\'button\' value=\' Apply \' onclick="applyFor(\'294\',\'Nutritional Services\',\'Food Service Operator\')" class=\'screenOnly ApplyButton\' /></span></td></tr></table><div style=\'position:relative;\'><li><span class=\'label\'>Position Type:</span><br/>&nbsp;&nbsp;<span class=\'normal\'>Nutritional Services/</span><span class=\'normal\'>Food Service Operator</span><br/><br/></li><li><span class="label" >Date Posted:</span><br/>&nbsp;&nbsp;<span class="normal">10/7/2026</span><br/><br/></li><li><span class="label" >Location:</span><br/>&nbsp;&nbsp;<span class="normal">Starr Elementary School</span><br/><br/></li><span>&nbsp&nbsp</span><span class="label" >Additional Information: &nbsp;</span><span class="normal"><a href="javascript:void(toggle_block(\'DescriptionText294_\'))"><u>Show/Hide</u></a></span><br/><span id=\'DescriptionText294_\' style=\'border: 1px solid black; padding:5px; display:none; opacity:.8;\'><span class="normal"><p><strong>Anderson School District Three has an immediate opening for a dependable and team-oriented Nutritional Services Operator at Starr Elementary School to support the daily preparation and service of healthy, high-quality meals for our students. This position assists with food preparation, service, sanitation, and maintaining a safe, welcoming cafeteria environment that promotes student well-being and success.</strong><br>&nbsp; &nbsp;&nbsp;<br><strong>Essential Duties:</strong><br>- Recognizes the components of a reimbursable meal.<br>- Assists in the preparation and serving of food.<br>- Adjusts recipes to the quantity required as well as adheres to recipes as provided.<br>- Maintains food safety by adhering to DHEC and District sanitation guidelines at all times.<br>- Maintains work area, equipment, floors and dining facilities in a clean and presentable manner.<br>- Has knowledge of equipment operations and appropriate chemicals used in cleaning and sanitizing; safely operates kitchen equipment.<br>- Ensures proper storage of both hot and cold foods to minimize the risk of food borne illness.<br>- Has the ability to physically perform the job responsibilities.<br>- Compiles and manages weekly invoices and required paperwork as required.<br>- Ability to work in a friendly manner with co-workers, students, and faculty/staff as well as additional district staff.<br>- Ability to adhere to Nutrition Services Dress Code to include appropriate uniform and shoes, hair restraint as well as DHEC and ServSafe guidelines.<br>- Ability to complete ServSafe training and pass the appropriate examinations.<br>- Performs other duties as directed.<br>&nbsp; &nbsp;&nbsp;<br><strong>Qualifications:</strong><br>- Experience working in Food Service, preferred<br>- Ability to read and comprehend recipes, work assignments, and in-service information<br>- Ability lift at least 50lbs, bend, stoop, and work on feet for an extended period of time<br>- Good organizational, math, and reading comprehension skills<br>- Ability to perform duties with awareness of all district, state, and federal requirements, as well as A3 Board Policies<br>&nbsp; &nbsp;&nbsp;<br><strong>Additional Information:</strong><br>&nbsp; &nbsp;&nbsp;<br><strong>REPORTS TO:</strong> Nutritional Services Manager&nbsp;with additional oversight from the Principal&nbsp;<br>&nbsp; &nbsp;&nbsp;<br><strong>TERMS OF EMPLOYMENT:</strong> 183 days @ 7.45 Hours Per Day&nbsp;<br>&nbsp; &nbsp;&nbsp;<br><strong>JOB TYPE:</strong> Full-time<br>&nbsp; &nbsp;&nbsp;<br><strong>SALARY:</strong> Food Service Operator Salary Schedule (Posted Online)&nbsp;<br>&nbsp; &nbsp;&nbsp;<br><strong>EVALUATION:</strong> Performance of this job will be evaluated annually by the Nutritional Services Manager&nbsp;<br><br><strong>FLSA STATUS:</strong> Non-Exempt<br><br><strong>Benefits:&nbsp;</strong><br>Eligible employees may enroll in health insurance, which includes prescription coverage and wellness benefits. Other available insurance benefits include dental, vision, term life insurance, long-term disability, and flexible spending accounts for health and child care expenses.<br><br>State employees are also offered retirement plan options, including defined benefits and defined contribution plans. Additionally, eligible employees may elect to participate in the South Carolina Deferred Compensation Program, which is a voluntary, supplemental retirement savings plan offering 401(k) and 457 plan options.<br><br>State employees may also be eligible for other benefits, including tuition assistance; holiday, annual and sick leave; and discounts on purchases, travel, and more.<br>&nbsp;</p></span><br /><img src=\'https://www.applitrack.com/clear.gif\' height=8><br /></span><br/><div class=\'label\' align=\'right\' style=\'position:absolute; top:0%;right:1%;\'><span class=\'s\'><div class="addthis_toolbox addthis_default_style "><a class="addthis_button_preferred_1"></a><a class="addthis_button_preferred_2"></a><a class="addthis_button_preferred_3"></a><a class="addthis_button_compact"></a><a class="addthis_counter addthis_bubble_style"></a></div><script type="text/javascript" src="https://s7.addthis.com/js/250/addthis_widget.js#pubid=ra-4dfeb954767d3dc8"></script><br><a href=\'javascript:updateEmailLinkFromCurrentWindowLocation("mailto:?subject=Job Posting&body=","I thought you would be interested in an employment opportunity I found at Anderson School District 3. The position is Nutritional Services Operator - Immediate Opening. Please click the link below for more information about this vacancy.","ApplitrackHardcodedURL?1=1&AppliTrackJobId=294&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1")\'><span style=\'color:#4c4c4c;font-size:.9em;font-weight:normal;\'>Email To A Friend</span></a><br/><a href=\'javascript:updateHrefFromCurrentWindowLocation("ApplitrackHardcodedURL?1=1&AppliTrackJobId=294&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1" , true, false, true)\'><span style=\'color:#4c4c4c;font-size:.9em;font-weight:normal;\'>Print Version</span> </a><br/></span></div><br/></div></ul><div style=\'width: 100%; height: .75px; background: gray; overflow: hidden; margin-bottom:4px;\'></div><br />');
function setUpPageBreaks(){h = 0; try{AppliTrack_$('p294_h').className = 'noprint'; if (h + AppliTrack_$('p294_').clientHeight > 775){h = 0; AppliTrack_$('p294_h').className = 'printOnly'; } h = h + AppliTrack_$('p294_').clientHeight; AppliTrack_$('p262_h').className = 'noprint'; if (h + AppliTrack_$('p262_').clientHeight > 775){h = 0; AppliTrack_$('p262_h').className = 'printOnly'; } h = h + AppliTrack_$('p262_').clientHeight; }catch(ex){}}
var h = 0
setTimeout("setUpPageBreaks()",100)
document.write('<p align=center class=noprint id=\'p262_h\'><span class=\'ListHeader\'>Openings as of 10/18/2026<br/></span></p><ul class=\'postingsList\' id=\'p262_\'><table class=\'title\' style=\'padding: 0px; width:99.2%\'><tr><td id=\'wrapword\' style=\'width: 950px; padding: 5px;\'>Substitute Bus Driver</td><td style=\'width: 275px;\'><span class=\'title2\' style=\'float:right;text-align:right;margin-top: 8px;margin-right: 6px;\'> JobID: 262 <input type=\'button\' value=\' Apply \' onclick="applyFor(\'262\',\'Transportation\',\'Bus Driver\')" class=\'screenOnly ApplyButton\' /></span></td></tr></table><div style=\'position:relative;\'><li><span class=\'label\'>Position Type:</span><br/>&nbsp;&nbsp;<span class=\'normal\'>Transportation/</span><span class=\'normal\'>Bus Driver</span><br/><br/></li><li><span class="label" >Date Posted:</span><br/>&nbsp;&nbsp;<span class="normal">12/9/2025</span><br/><br/></li><li><span class="label" >Location:</span><br/>&nbsp;&nbsp;<span class="normal">Transportation Department</span><br/><br/></li><span>&nbsp&nbsp</span><span class="label" >Additional Information: &nbsp;</span><span class="normal"><a href="javascript:void(toggle_block(\'DescriptionText262_\'))"><u>Show/Hide</u></a></span><br/><span id=\'DescriptionText262_\' style=\'border: 1px solid black; padding:5px; display:none; opacity:.8;\'><span class="normal"><dl> &nbsp;&nbsp;&nbsp;&nbsp;<dd><p><strong>Anderson School District Three is seeking reliable and safety-conscious individuals to serve as part-time Substitute Bus Drivers. This position involves safely transporting students to and from school and school-related activities while maintaining a positive and orderly bus environment. Applicants must meet all state licensing and certification requirements.</strong><br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<span style="font-family:arial,helvetica,sans-serif;"><strong><u>Qualifications</u></strong></span><br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<span style="font-family:arial,helvetica,sans-serif;"><strong>Education:</strong><br data-pasted="true" /> &nbsp;&nbsp;&nbsp;&nbsp;- Minimum High School Diploma or GED<br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<strong>Knowledge &amp; Skills:</strong><br /> &nbsp;&nbsp;&nbsp;&nbsp;-&nbsp;</span>Meet all requirements to obtain a Commercial Driver&rsquo;s License (CDL)<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Be at least 18 years of age.<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Submit to random alcohol and drug testing<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Meet all physical, mental, and moral requirements<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Meet all requirements to obtain a CDL Class B with Air Brakes, Passenger &amp; School Bus Endorsements<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Have no more than 4 points against current driving record in previous twelve months<br /> &nbsp;&nbsp;&nbsp;&nbsp;- License may not be suspended for a moving violation within the previous 5 years<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Successfully complete the 20 hours South Carolina School Bus Training Class (held monthly).<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Successfully complete 20 hours of Behind the Wheel Training<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Pass a Department of Transportation physical examination<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Test negative for alcohol and drugs<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Have a minimum of twelve (12) months driving experience<br /> &nbsp;&nbsp;&nbsp;&nbsp;- Submit to a criminal background check.<br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<span style="font-family:arial,helvetica,sans-serif;"><strong><u>Additional Information</u></strong></span><br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<span style="font-family:arial,helvetica,sans-serif;"><strong>REPORTS TO:</strong>&nbsp;Director of Transporation<br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<b>SALARY:&nbsp;</b>Bus Driver Salary Schedule (Posted Online)&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<strong>JOB TYPE:</strong>&nbsp;Part-time<br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<strong>EVALUATION:</strong> Performance of this job will be evaluated by the Director of Transporation&nbsp;</span><br /> &nbsp;&nbsp;&nbsp;&nbsp;<br /> &nbsp;&nbsp;&nbsp;&nbsp;<strong>FLSA STATUS:</strong> Non-Exempt</p> &nbsp;&nbsp;&nbsp;&nbsp;</dd> </dl> </span><br /><img src=\'https://www.applitrack.com/clear.gif\' height=8><br /></span><br/><div class=\'label\' align=\'right\' style=\'position:absolute; top:0%;right:1%;\'><span class=\'s\'><div class="addthis_toolbox addthis_default_style "><a class="addthis_button_preferred_1"></a><a class="addthis_button_preferred_2"></a><a class="addthis_button_preferred_3"></a><a class="addthis_button_compact"></a><a class="addthis_counter addthis_bubble_style"></a></div><script type="text/javascript" src="https://s7.addthis.com/js/250/addthis_widget.js#pubid=ra-4dfeb954767d3dc8"></script><br><a href=\'javascript:updateEmailLinkFromCurrentWindowLocation("mailto:?subject=Job Posting&body=","I thought you would be interested in an employment opportunity I found at Anderson School District 3. The position is Substitute Bus Driver. Please click the link below for more information about this vacancy.","ApplitrackHardcodedURL?1=1&AppliTrackJobId=262&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1")\'><span style=\'color:#4c4c4c;font-size:.9em;font-weight:normal;\'>Email To A Friend</span></a><br/><a href=\'javascript:updateHrefFromCurrentWindowLocation("ApplitrackHardcodedURL?1=1&AppliTrackJobId=262&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1" , true, false, true)\'><span style=\'color:#4c4c4c;font-size:.9em;font-weight:normal;\'>Print Version</span> </a><br/></span></div><br/></div></ul><div style=\'width: 100%; height: .75px; background: gray; overflow: hidden; margin-bottom:4px;\'></div><br /></div><span class=\'screenOnly\'><div class="subnotetext"></div></span><noscript>Powered by Aspex Solutions - AppliTrack, Applicant Tracking for Educators. Online Job Employment Applications, Web Based Employment Applications for School Districts and Educational Institutions - <a href=http://www.aspexsolutions.com/home/>www.aspexsolutions.com</a></noscript>');
document.write('<noscript>You can also view <a href=\'http://www.k12jobspot.com/south carolina-teaching-jobs\'>South Carolina teaching jobs</a> at www.k12jobspot.com. K12JobSpot is a site that has thousands of teaching jobs - all from AppliTrack school districts.</noscript></form></div>');
var gaJsHost = (("https:" == document.location.protocol) ? "https://ssl." : "http://www."); document.write(unescape("%3Cscript src='" + gaJsHost + "google-analytics.com/ga.js' type='text/javascript'%3E%3C/script%3E"));try {var pageTracker = _gat._getTracker("UA-12436368-1");pageTracker._trackPageview();} catch(err) {}