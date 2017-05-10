import os
import datetime

from pupa.scrape import Scraper, Bill, VoteEvent
from pupa.scrape.base import ScrapeError

import xlrd
import scrapelib
import lxml.html
import pytz


class OHBillScraper(Scraper):
    _tz = pytz.timezone('US/Eastern')

    def scrape(self, session=None, chambers=None):
        # Bills endpoint can sometimes take a very long time to load
        self.timeout = 300

        if not session:
            session = self.latest_session()
            self.info('no session, using %s', session)

        if int(session) < 128:
            raise AssertionError("No data for period {}".format(session))

        elif int(session) < 131:
            # they changed their data format starting in 131st and added
            # an undocumented API
            yield from self.old_scrape(session)

        else:
            chamber_dict = {"Senate": "upper", "House": "lower",
                            "House of Representatives": "lower",
                            "house": "lower", "senate": "upper"}

            # so presumanbly not everything passes, but we haven't
            # seen anything not pass yet, so we'll need to wait
            # till it fails and get the right language in here
            vote_results = {"approved": True,
                            "passed": True,
                            "adopted": True,
                            "true": True,
                            "false": False,
                            "failed": False,
                            True: True,
                            False: False}

            action_dict = {"ref_ctte_100": "referral-committee",
                           "intro_100": "introduction",
                           "pass_300": "passage",
                           "intro_110": "reading-1",
                           "refer_210": "referral-committee",
                           "crpt_301": None,
                           "crpt_317": None,
                           "concur_606": "passage",
                           "pass_301": "passage",
                           "refer_220": "referral-committee",
                           "intro_102": ["introduction", "passage"],
                           "intro_105": ["introduction", "passage"],
                           "intro_ref_ctte_100": "referral-committee",
                           "refer_209": None,
                           "intro_108": ["introduction", "passage"],
                           "intro_103": ["introduction", "passage"],
                           "msg_reso_503": "passage",
                           "intro_107": ["introduction", "passage"],
                           "imm_consid_360": "passage",
                           "refer_213": None,
                           "adopt_reso_100": "passage",
                           "msg_507": "amendment-passage",
                           "confer_713": None,
                           "concur_603": None,
                           "confer_712": None,
                           "msg_506": "amendment-failure",
                           "receive_message_100": "passage",
                           "motion_920": None,
                           "concur_611": None,
                           "confer_735": None
                           }

            base_url = "http://search-prod.lis.state.oh.us"
            first_page = base_url
            first_page += "/solarapi/v1/general_assembly_{session}/".format(session=session)
            legislators = self.get_legislator_ids(first_page)
            all_amendments = self.get_other_data_source(first_page, base_url, "amendments")
            all_fiscals = self.get_other_data_source(first_page, base_url, "fiscals")
            all_synopsis = self.get_other_data_source(first_page, base_url, "synopsiss")
            all_analysis = self.get_other_data_source(first_page, base_url, "analysiss")
            doc_types = ["bills", "resolutions"]
            for doc_type in doc_types:
                bill_versions = {}
                for doc in self.pages(base_url, first_page+doc_type):
                    for v in doc["items"]:
                        # bills is actually a list of versions
                        # going to create a dictionary as follows:
                        # key=bill_id
                        # value = dict of all versions, where keys are versionids
                        # and values are the bill data from the api.
                        # then loop through that to avoid duplicate bills

                        try:
                            bill_id = v["number"]
                        except KeyError:
                            self.warning("Apparent bill has no information:\n{}".format(v))
                            continue

                        version_id = v["versionid"]
                        if bill_id in bill_versions:
                            if version_id in bill_versions[bill_id]:
                                self.logger.warning("There are two versions of {bill_id}"
                                                    " called the same thing."
                                                    " Bad news bears!".format(bill_id=bill_id))
                            else:
                                bill_versions[bill_id][version_id] = v
                        else:
                            bill_versions[bill_id] = {}
                            bill_versions[bill_id][version_id] = v

                for b in bill_versions:
                    bill = None
                    for bill_version in bill_versions[b].values():
                        if not bill:
                            bill_id = bill_version["number"]
                            title = bill_version["shorttitle"] or bill_version["longtitle"]
                            title = title.strip()

                            if not len(title):
                                self.warning("Missing title for {bill_id}".format(bill_id=bill_id))
                                next

                            chamber = "lower" if "h" in bill_id else "upper"

                            subjects = []
                            for subj in bill_version["subjectindexes"]:
                                try:
                                    subjects.append(subj["primary"])
                                except KeyError:
                                    pass
                                try:
                                    secondary_subj = subj["secondary"]
                                except KeyError:
                                    secondary_subj = ""
                                if secondary_subj:
                                    subjects.append(secondary_subj)

                            # they use bill id of format HB 1 on the site
                            # but hb1 in the API.

                            for idx, char in enumerate(bill_id):
                                try:
                                    int(char)
                                except ValueError:
                                    continue

                                display_id = bill_id[:idx]+" "+bill_id[idx:]
                                break
                            classification = {'bills': 'bill',
                                              'resolutions': 'resolution'}[doc_type]
                            bill = Bill(display_id.upper(),
                                        legislative_session=session,
                                        chamber=chamber,
                                        title=title,
                                        classification=classification
                                        )

                            for subject in subjects:
                                bill.add_subject(subject)
                            # this stuff is the same for all versions

                            bill.add_source(first_page+doc_type+"/"+bill_id)

                            sponsors = bill_version["sponsors"]
                            for sponsor in sponsors:
                                sponsor_name = self.get_sponsor_name(sponsor)
                                bill.add_sponsorship(
                                                    sponsor_name,
                                                    classification='primary',
                                                    entity_type='person',
                                                    primary=True
                                    )

                            cosponsors = bill_version["cosponsors"]
                            for sponsor in cosponsors:
                                sponsor_name = self.get_sponsor_name(sponsor)
                                bill.add_sponsorship(
                                                     sponsor_name,
                                                     classification='cosponsor',
                                                     entity_type='person',
                                                     primary=False,
                                    )
                            try:
                                action_doc = self.get(base_url+bill_version["action"][0]["link"])
                            except scrapelib.HTTPError:
                                pass
                            else:

                                actions = action_doc.json()
                                for action in reversed(actions["items"]):
                                    actor = chamber_dict[action["chamber"]]
                                    action_desc = action["description"]
                                    try:
                                        action_type = action_dict[action["actioncode"]]
                                    except KeyError:
                                        self.warning("Unknown action {desc} with code {code}."
                                                     " Add it to the action_dict"
                                                     ".".format(desc=action_desc,
                                                                code=action["actioncode"]))
                                        action_type = None

                                    date = self._tz.localize(datetime.datetime.strptime(
                                                             action["datetime"],
                                                             "%Y-%m-%dT%H:%M:%S"))
                                    date = "{:%Y-%m-%d}".format(date)

                                    bill.add_action(action_desc,
                                                    date, chamber=actor,
                                                    classification=action_type)
                            self.add_document(all_amendments, bill_id, "amendment", bill, base_url)
                            self.add_document(all_fiscals, bill_id, "fiscal", bill, base_url)
                            self.add_document(all_synopsis, bill_id, "synopsis", bill, base_url)
                            self.add_document(all_analysis, bill_id, "analysis", bill, base_url)

                            vote_url = base_url+bill_version["votes"][0]["link"]
                            vote_doc = self.get(vote_url)
                            votes = vote_doc.json()
                            yield from self.process_vote(votes, vote_url,
                                                         base_url, bill, legislators,
                                                         chamber_dict, vote_results)

                            vote_url = base_url
                            vote_url += bill_version["cmtevotes"][0]["link"]
                            try:
                                vote_doc = self.get(vote_url)
                            except scrapelib.HTTPError:
                                self.warning("Vote page not "
                                             "loading; skipping: {}".format(vote_url))
                                continue
                            votes = vote_doc.json()
                            yield from self.process_vote(votes, vote_url,
                                                         base_url, bill, legislators,
                                                         chamber_dict, vote_results)

                            # we have never seen a veto or a disapprove, but they seem important.
                            # so we'll check and throw an error if we find one
                            # life is fragile. so are our scrapers.
                            if "veto" in bill_version:
                                veto_url = base_url+bill_version["veto"][0]["link"]
                                veto_json = self.get(veto_url).json()
                                if len(veto_json["items"]) > 0:
                                    raise AssertionError("Whoa, a veto! We've never"
                                                         " gotten one before."
                                                         " Go write some code to deal"
                                                         " with it: {}".format(veto_url))

                            if "disapprove" in bill_version:
                                disapprove_url = base_url+bill_version["disapprove"][0]["link"]
                                disapprove_json = self.get(disapprove_url).json()
                                if len(disapprove_json["items"]) > 0:
                                    raise AssertionError("Whoa, a disapprove! We've never"
                                                         " gotten one before."
                                                         " Go write some code to deal "
                                                         "with it: {}".format(disapprove_url))

                        # this stuff is version-specific
                        version_name = bill_version["version"]
                        version_link = base_url+bill_version["pdfDownloadLink"]
                        if version_link.endswith("pdf"):
                            mimetype = "application/pdf"
                        else:
                            mimetype = "application/octet-stream"
                        bill.add_version_link(version_name, version_link, media_type=mimetype)
                    # Need to sort bill actions, since they may be jumbled

                    yield bill

    def pages(self, base_url, first_page):
        page = self.get(first_page)
        page = page.json()
        yield page
        while "nextLink" in page:
            page = self.get(base_url+page["nextLink"])
            page = page.json()
            yield page

    def get_other_data_source(self, first_page, base_url, source_name):
        # produces a dictionary from bill_id to a list of
        # one of the following:
        # amendments, analysis, fiscals, synopsis
        # could pull these by bill, but doing it in bulk
        # and then matching on our end will get us by with way fewer
        # api calls

        bill_dict = {}
        for page in self.pages(base_url, first_page+source_name):
            for item in page["items"]:
                billno = item["billno"]
                if billno not in bill_dict:
                    bill_dict[billno] = []
                bill_dict[billno].append(item)

        return bill_dict

    def add_document(self, documents, bill_id, type_of_document, bill, base_url):
        try:
            documents = documents[bill_id]
        except KeyError:
            return

        leg_ver_types = {"IN": "Introduction",
                         "RS": "Reported: Senate",
                         "PS": "Passed: Senate",
                         "RH": "Reported: House",
                         "PH": "Passed: House",
                         "": "",
                         "ICS": "",
                         "IC": "",
                         "RCS": "",
                         "EN": "Enacted",
                         "RCH": "Re-referred",
                         "RRH": "",
                         "PHC": "",
                         "CR": ""
                         }

        for item in documents:
            if type_of_document == "amendment":
                name = item["amendnum"] + " " + item["version"]
            else:
                name = item["name"] or type_of_document
            link = base_url+item["link"]+"?format=pdf"
            try:
                self.head(link)
            except scrapelib.HTTPError:
                self.logger.warning("The link to doc {name}"
                                    " does not exist, skipping".format(name=name))
                continue
            if "legacyver" in item:
                try:
                    ver = leg_ver_types[item["legacyver"]]
                except KeyError:
                    self.logger.warning(
                        "New legacyver; check the type and add it to the "
                        "leg_ver_types dictionary: {} ({})".format(
                            item["legacyver"], item['link']))
                    ver = ""
                if ver:
                    name = name+": "+ver
            bill.add_document_link(name, link, media_type="application/pdf")

    def get_legislator_ids(self, base_url):
        legislators = {}
        for chamber in ["House", "Senate"]:
            url = base_url+"chamber/{chamber}/legislators?per_page=100"
            doc = self.get(url.format(chamber=chamber))
            leg_json = doc.json()
            for leg in leg_json["items"]:
                legislators[leg["med_id"]] = leg["displayname"]

        return legislators

    def get_sponsor_name(self, sponsor):
        return " ".join([sponsor["firstname"], sponsor["lastname"]])

    def process_vote(self, votes, url, base_url, bill, legislators, chamber_dict, vote_results):
        for v in votes["items"]:
            try:
                v["yeas"]
            except KeyError:
                # sometimes the actual vote is buried a second layer deep
                v = self.get(base_url+v["link"]).json()
                try:
                    v["yeas"]
                except KeyError:
                    self.logger.warning("No vote info available, skipping")
                    continue

            try:
                chamber = chamber_dict[v["chamber"]]
            except KeyError:
                chamber = "lower" if "house" in v["apn"] else "upper"
            try:
                date = self._tz.localize(datetime.datetime.strptime(v["date"], "%m/%d/%y"))
                date = "{:%Y-%m-%d}".format(date)
            except KeyError:
                try:
                    date = self._tz.localize(datetime.datetime.strptime(v["occurred"], "%m/%d/%y"))
                    date = "{:%Y-%m-%d}".format(date)
                except KeyError:
                    self.logger.warning("No date found for vote, skipping")
                    continue
            try:
                motion = v["action"]
            except KeyError:
                motion = v["motiontype"]

            # Sometimes Ohio's SOLAR will only return part of the JSON, so in that case skip
            if (not motion and isinstance(v['yeas'], str)
               and isinstance(v['nays'], str)):
                waringText = 'Malformed JSON found for vote ("revno" of {}); skipping'
                self.warning(waringText.format(v['revno']))
                continue

            result = v.get("results") or v.get("passed")
            if result is None:
                if len(v['yeas']) > len(v['nays']):
                    result = "passed"
                else:
                    result = "failed"

            passed = vote_results[result.lower()]
            if "committee" in v:
                vote = VoteEvent(chamber=chamber,
                                 start_date=date,
                                 motion_text=motion,
                                 result='pass' if passed else 'fail',
                                 # organization=v["committee"],
                                 bill=bill,
                                 classification='passed'
                                 )
            else:
                vote = VoteEvent(chamber=chamber,
                                 start_date=date,
                                 motion_text=motion,
                                 result='pass' if passed else 'fail',
                                 classification='passed',
                                 bill=bill
                                 )
            # the yea and nay counts are not displayed, but vote totals are
            # and passage status is.
            yes_count = 0
            no_count = 0
            absent_count = 0
            excused_count = 0
            for voter_id in v["yeas"]:
                vote.yes(legislators[voter_id])
                yes_count += 1
            for voter_id in v["nays"]:
                vote.no(legislators[voter_id])
                no_count += 1
            if "absent" in v:
                for voter_id in v["absent"]:
                    vote.vote('absent', legislators[voter_id])
                    absent_count += 1
            if "excused" in v:
                for voter_id in v["excused"]:
                    vote.vote('excused', legislators[voter_id])
                    excused_count += 1

            vote.set_count('yes', yes_count)
            vote.set_count('no', no_count)
            vote.set_count('absent', absent_count)
            vote.set_count('excused', excused_count)
            # check to see if there are any other things that look
            # like vote categories, throw a warning if so
            for key, val in v.items():
                if (type(val) == list and len(val) > 0 and
                   key not in ["yeas", "nays", "absent", "excused"]):
                    if val[0] in legislators:
                        self.logger.warning("{k} looks like a vote type that's not being counted."
                                            " Double check it?".format(k=key))
            vote.add_source(url)

            yield vote

    def old_scrape(self, session=None):
        status_report_url = "http://www.legislature.ohio.gov/legislation/status-reports"

        # ssl verification off due Ohio not correctly implementing SSL
        if not session:
            session = self.latest_session()
            self.info('no session, using %s', session)

        doc = self.get(status_report_url, verify=False).text
        doc = lxml.html.fromstring(doc)
        doc.make_links_absolute(status_report_url)
        xpath = "//div[contains(text(),'{}')]/following-sibling::table"
        status_table = doc.xpath(xpath.format(session))[0]
        status_links = status_table.xpath(".//a[contains(text(),'Excel')]/@href")

        for url in status_links:

            try:
                fname, resp = self.urlretrieve(url)
            except scrapelib.HTTPError as report:
                self.logger.warning("Missing report {}".format(report))
                continue

            sh = xlrd.open_workbook(fname).sheet_by_index(0)

            # once workbook is open, we can remove tempfile
            os.remove(fname)
            for rownum in range(1, sh.nrows):
                bill_id = sh.cell(rownum, 0).value

                bill_type = "resolution" if "R" in bill_id else "bill"
                chamber = "lower" if "H" in bill_id else "upper"

                bill_title = str(sh.cell(rownum, 3).value)

                bill = Bill(
                            bill_id,
                            legislative_session=session,
                            chamber=chamber,
                            title=bill_title,
                            classification=bill_type
                       )
                bill.add_source(url)
                bill.add_sponsor('primary', str(sh.cell(rownum, 1).value))

                # add cosponsor
                if sh.cell(rownum, 2).value:
                    bill.add_sponsor('cosponsor',
                                     str(sh.cell(rownum, 2).value))

                actor = ""

                # Actions start column after bill title
                for colnum in range(4, sh.ncols - 1):
                    action = str(sh.cell(0, colnum).value)
                    cell = sh.cell(rownum, colnum)
                    date = cell.value

                    if len(action) != 0:
                        if action.split()[0] == 'House':
                            actor = "lower"
                        elif action.split()[0] == 'Senate':
                            actor = "upper"
                        elif action.split()[-1] == 'Governor':
                            actor = "executive"
                        elif action.split()[0] == 'Gov.':
                            actor = "executive"
                        elif action.split()[-1] == 'Gov.':
                            actor = "executive"

                    if action in ('House Intro. Date', 'Senate Intro. Date'):
                        atype = ['bill:introduced']
                        action = action.replace('Intro. Date', 'Introduced')
                    elif action == '3rd Consideration':
                        atype = ['bill:reading:3', 'bill:passed']
                    elif action == 'Sent to Gov.':
                        atype = ['governor:received']
                    elif action == 'Signed By Governor':
                        atype = ['governor:signed']
                    else:
                        atype = ['other']

                    if type(date) == float:
                        date = str(xlrd.xldate_as_tuple(date, 0))
                        date = datetime.datetime.strptime(
                            date, "(%Y, %m, %d, %H, %M, %S)")
                        date = self._tz.localize(date)
                        date = "{:%Y-%m-%d}".format(date)
                        bill.add_action(actor, action, date, type=atype)

                for idx, char in enumerate(bill_id):
                    try:
                        int(char)
                    except ValueError:
                        continue

                    underscore_bill = bill_id[:idx]+"_"+bill_id[idx:]
                    break

                yield from self.scrape_votes_old(bill, underscore_bill, session)
                self.scrape_versions_old(bill, underscore_bill, session)
                yield bill

    def scrape_versions_old(self, bill, billname, session):
        base_url = 'http://archives.legislature.state.oh.us/'

        if 'R' in billname:
            piece = '/res.cfm?ID=%s_%s' % (session, billname)
        else:
            piece = '/bills.cfm?ID=%s_%s' % (session, billname)

        def _get_html_or_pdf_version_old(url):
            doc = lxml.html.fromstring(url)
            name = doc.xpath('//font[@size="2"]/a/text()')[0]
            html_links = doc.xpath('//a[text()="(.html format)"]')
            pdf_links = doc.xpath('//a[text()="(.pdf format)"]')
            if html_links:
                link = html_links[0].get('href')
                bill.add_version_link(name, base_url + link, on_duplicate='use_old',
                                      media_type='text/html')
            elif pdf_links:
                link = pdf_links[0].get('href')
                bill.add_version_link(name, base_url + link,
                                      media_type='application/pdf')

        html = self.get(base_url + piece).text
        # pass over missing bills - (unclear why this happens)
        if 'could not be found.' in html:
            self.warning('missing page: %s' % base_url + piece)
            return

        _get_html_or_pdf_version_old(html)
        doc = lxml.html.fromstring(html)
        for a in doc.xpath('//a[starts-with(@href, "/bills.cfm")]/@href'):
            if a != piece:
                _get_html_or_pdf_version_old(self.get(base_url + a).text)
        for a in doc.xpath('//a[starts-with(@href, "/res.cfm")]/@href'):
            if a != piece:
                _get_html_or_pdf_version_old(self.get(base_url + a).text)

    def scrape_votes_old(self, bill, billname, session):
        vote_url = ('http://archives.legislature.state.oh.us/bills.cfm?ID=' +
                    session + '_' + billname)

        page = self.get(vote_url).text
        page = lxml.html.fromstring(page)

        for jlink in page.xpath("//a[contains(@href, 'JournalText')]"):
            date = self._tz.localize(datetime.datetime.strptime(jlink.text,
                                                                "%m/%d/%Y")).date()
            date = "{:%Y-%m-%d}".format(date)
            details = jlink.xpath("string(../../../td[2])")

            chamber = details.split(" - ")[0]
            if chamber == 'House':
                chamber = 'lower'
            elif chamber == 'Senate':
                chamber = 'upper'
            else:
                raise ScrapeError("Bad chamber: %s" % chamber)

            motion = details.split(" - ")[1].split("\n")[0].strip()

            vote_row = jlink.xpath("../../..")[0].getnext()

            yea_div = vote_row.xpath(
                "td/font/div[contains(@id, 'Yea')]")[0]
            yeas = []
            for td in yea_div.xpath("table/tr/td"):
                name = td.xpath("string()")
                if name:
                    yeas.append(name)

            no_div = vote_row.xpath(
                "td/font/div[contains(@id, 'Nay')]")[0]
            nays = []
            for td in no_div.xpath("table/tr/td"):
                name = td.xpath("string()")
                if name:
                    nays.append(name)

            yes_count = len(yeas)
            no_count = len(nays)

            vote = VoteEvent(
                        chamber=chamber,
                        start_date=date,
                        motion_text=motion,
                        result='pass' if yes_count > no_count else 'fail',
                        bill=bill,
                        classification='passed'
                   )

            for yes in yeas:
                vote.yes(yes)
            for no in nays:
                vote.no(no)

            vote.add_source(vote_url)

            yield vote
