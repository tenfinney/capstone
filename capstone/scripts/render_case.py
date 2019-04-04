from contextlib import contextmanager
from copy import deepcopy

from lxml import etree, sax


### HELPERS ###

def iter_pars(opinions):
    """ Yield all paragraph dicts from a list of opinions. """
    for opinion in opinions:
        yield from opinion.get('paragraphs', [])
        for footnote in opinion.get('footnotes', []):
            yield from footnote.get('paragraphs', [])

not_redacted_tokens = {'font'}  # these formatting tokens shouldn't be stripped from redacted spans, because it messes up rendering

def filter_tokens(tokens, tags, redacted=True):
    """
        Filter a list of tokens and yield only text strings and tags included in `tags`. If redacted=True, filter out
        everything between ['redact'] tags. Example:
        >>> list(filter_tokens(['text', ['foo'], ['bar'], ['redact'], 'text2', ['redact']], {'foo'}))
        ['text', ['foo']]
    """
    if tags is None:
        yield from tokens
        return
    redacted_span = False
    for token in tokens:
        if redacted and redacted_span:
            if type(token) != str:
                if token[0] == '/redact':
                    redacted_span = False
                elif token[0].lstrip('/') in not_redacted_tokens and token[0].lstrip('/') in tags:
                    yield token
        elif type(token) == str:
            yield token
        elif redacted and token[0] == 'redact':
            redacted_span = True
        elif token[0].lstrip('/') in tags:
            yield token

class VolumeRenderer:
    """
        Class to render:
            - <Page> element from ALTO
            - <volume> element from volmets
            - <case> element from casemets
            - <casebody> element from casemets:
                - as original XML matching delivered files
                - as updated XML with <em> and <page-number>
                - as HTML
                - as text
                - as token stream for debugging
    """
    def __init__(self, blocks_by_id, fonts_by_id, labels_by_block_id, redacted=True):
        """ Set some initial values that apply for the whole volume. """
        self.blocks_by_id = blocks_by_id
        self.fonts_by_id = fonts_by_id
        self.labels_by_block_id = labels_by_block_id
        self.redacted = redacted
        self.original_xml = False


    ### ALTO <Page> RENDERING ###

    # these tags make it into the ALTO rendering
    alto_block_token_filter = {'line', 'ocr', 'font', 'edit'}

    def render_page(self, page, redacted):
        """
            Render <Page> element from ALTO
        """
        # <Page>
        page_el = etree.Element("Page", {
            'HEIGHT': str(page.height),
            'WIDTH': str(page.width),
            'xmlns': "http://www.loc.gov/standards/alto/ns-v3#",
        })
        fonts_lookup = page.font_names
        space_el = None
        space_index = None

        # create empty space_el for cases with no blocks
        if not page.blocks:
            space_rect = page.spaces[0] if page.spaces else [0, 0, page.width, page.height]
            space_el = etree.SubElement(page_el, 'PrintSpace', self.rect_to_dict(space_rect))

        for block in page.blocks:
            # <PrintSpace>
            if space_el is None or space_index != block.get('space'):
                space_index = block.get('space')
                space_rect = page.spaces[space_index] if space_index is not None else [0, 0, page.width, page.height]
                space_el = etree.SubElement(page_el, 'PrintSpace', self.rect_to_dict(space_rect))

            if redacted and block.get('redacted', False):
                continue

            # <Illustration>
            if block.get('format') == 'image':
                etree.SubElement(space_el, 'Illustration', dict(ID=block['id'], **self.rect_to_dict(block['rect'])))

            # <TextBlock>
            else:
                block_el = etree.SubElement(space_el, 'TextBlock', dict(ID=block['id'], **self.rect_to_dict(block['rect'])))
                current_font = None
                ignore_strings = False
                string_el = None
                line_el = None
                for token in filter_tokens(block['tokens'], self.alto_block_token_filter, redacted):
                    if type(token) == str:
                        if not ignore_strings:
                            string_el.attrib['CONTENT'] += token
                    elif token[0] == 'line':
                        if string_el is not None:
                            self.close_string(string_el)
                            string_el = None
                        line_el = etree.SubElement(block_el, 'TextLine', self.rect_to_dict(token[1]['rect']))
                    elif token[0] == 'font':
                        current_font = fonts_lookup[token[1]['id']]
                    elif token[0] == 'edit':
                        string_el.attrib['CONTENT'] += token[1]['was']
                        ignore_strings = True
                    elif token[0] == '/edit':
                        ignore_strings = False
                    elif token[0] == 'ocr':
                        if string_el is not None:
                            self.close_string(string_el)
                            etree.SubElement(line_el, 'SP')
                        string_el = etree.SubElement(line_el, 'String', dict(
                            CONTENT="",
                            STYLEREFS=current_font,
                            CC=str(token[1].get('cc',0)),
                            WC=('%.2f' % token[1]['wc']),
                            **self.rect_to_dict(token[1]['rect'])))
                if string_el is not None:
                    self.close_string(string_el)

        return etree.tostring(page_el, encoding=str)

    def close_string(self, string_el):
        """ Once <String CONTENT> is reassembled, strip any closing spaces and reconstruct CC value. """
        content = string_el.attrib['CONTENT']
        if content.endswith(' '):
            content = string_el.attrib['CONTENT'] = content[:-1]
        if content:
            # convert integer value for character confidence, like 0b10101010, back into a string like '90909090', by
            # converting integer to binary string, left padding with zeroes, and replacing 1 with 9
            string_el.attrib['CC'] = bin(int(string_el.attrib['CC']))[2:].zfill(len(string_el.attrib['CONTENT'])).replace('1', '9')
        else:
            string_el.attrib['CC'] = '0'

    def rect_to_dict(self, rect):
        """ Convert [left, top, width, height] back into attributes. """
        return {'HPOS': str(rect[0]), 'VPOS': str(rect[1]), 'WIDTH': str(rect[2]), 'HEIGHT': str(rect[3])}

    ### VOLUME <volume> RENDERING ###

    def render_volume(self, volume):
        """
            Render <volume> element from volmets
        """
        metadata = volume.xml_metadata
        volume_el = etree.Element('volume', {
            'barcode': volume.barcode,
            'contributinglibrary': metadata['contributing_library'],
            'xmlns': 'http://nrs.harvard.edu/urn-3:HLS.Libr.US_Case_Law.Schema.Volume:v1',
        })
        reporter = metadata['reporter']
        etree.SubElement(volume_el, 'reporter', {'abbreviation': reporter['abbreviation'], 'volnumber': reporter['volume_number']}).text = reporter['name']
        for reporter in metadata.get('nominative_reporters', []):
            etree.SubElement(volume_el, 'nominativereporter', {'abbreviation': reporter['abbreviation'], 'volnumber': reporter['volume_number']}).text = reporter['name']
        voldate_el = etree.SubElement(volume_el, 'voldate')
        etree.SubElement(voldate_el, 'start').text = metadata['start_date']
        if metadata['end_date']:
            etree.SubElement(voldate_el, 'end').text = metadata['end_date']
        if metadata['spine_start_date']:
            spinedate_el = etree.SubElement(volume_el, 'spinedate')
            etree.SubElement(spinedate_el, 'start').text = metadata['spine_start_date']
            if metadata['end_date']:
                etree.SubElement(spinedate_el, 'end').text = metadata['spine_end_date']
        etree.SubElement(volume_el, 'publicationdate').text = metadata['publication_date']
        publisher = metadata['publisher']
        etree.SubElement(volume_el, 'publisher', {'place': publisher['place']}).text = publisher['name']

        return etree.tostring(volume_el, encoding=str)

    ### CASE <case> RENDERING ###

    def render_case_header(self, case_id, metadata):
        """
            Render <case> element from casemets
        """
        case_el = etree.Element('case', {
            'caseid': case_id,
            'publicationstatus': metadata['status'],
            'xmlns': 'http://nrs.harvard.edu/urn-3:HLS.Libr.US_Case_Law.Schema.Case:v1',
        })
        etree.SubElement(case_el, 'court', {'abbreviation': metadata['court']['abbreviation'], 'jurisdiction': metadata['court']['jurisdiction']}).text = metadata['court']['name']
        if 'district' in metadata:
            etree.SubElement(case_el, 'district', {'abbrevation': metadata['district']['abbreviation']}).text = metadata['district']['name']
        etree.SubElement(case_el, 'name', {'abbreviation': metadata['name_abbreviation']}).text = metadata['name']
        for docket_number in metadata['docket_numbers']:
            etree.SubElement(case_el, 'docketnumber').text = docket_number
        for citation in metadata['citations']:
            etree.SubElement(case_el, 'citation', {'category': citation['category'], 'type': citation['type']}).text = citation['text']
        etree.SubElement(case_el, 'decisiondate').text = metadata['decision_date']
        if 'argument_date' in metadata:
            etree.SubElement(case_el, 'argumentdate').text = metadata['argument_date']

        return etree.tostring(case_el, encoding=str)

    ### TEXT RENDERING ###

    def render_text(self, case):
        """
            Render <casebody> as plain text
        """
        case_structure = case.structure
        pars = []
        for par in iter_pars(case_structure.opinions):
            if par.get("redacted"):
                continue
            words = []
            for block_id in par['block_ids']:
                block = self.blocks_by_id[block_id]
                if block.get("redacted"):
                    continue
                words.extend(filter_tokens(block.get('tokens', []), {}))
            pars.append("".join(words))
        if case_structure.corrections:
            pars.extend(case_structure.corrections)
        return "\n\n".join(pars)

    ### XML/HTML RENDERING ###

    html_token_filter = {'footnotemark', 'bracketnum', 'font'}
    font_style_map = (('em', 'italics'),)  # ('strong', 'bold')

    def render_html(self, case):
        """
            Render <casebody> as HTML
        """
        self.format = 'html'
        return self.render_markup(case)

    def render_xml(self, case):
        """
            Render <casebody> as XML, with <em> and <page-number>
        """
        self.format = 'xml'
        self.original_xml = False
        return self.render_markup(case)

    def render_orig_xml(self, case):
        """
            Render <casebody> as XML, matching original format from Innodata
        """
        self.format = 'xml'
        self.original_xml = True
        return self.render_markup(case)

    def hydrate_opinions(self, opinions, blocks_by_id):
        """
            Render <casebody> as token stream for debugging
        """
        opinions = deepcopy(opinions)
        for par in iter_pars(opinions):
            par['blocks'] = [blocks_by_id[id] for id in par['block_ids']]
        return opinions

    def render_markup(self, case):
        """
            Core renderer.
        """
        case_structure = case.structure
        self.opinions = case_structure.opinions
        self.corrections = case_structure.corrections
        self.duplicative = case_structure.metadata.duplicative

        # <section class='case'>, or <casebody>
        case_el = self.make_case_el(case)
        last_page_label = None
        for opinion in self.opinions:

            # <section class='opinion'>, or <opinion>
            opinion_el = self.make_opinion_el(opinion)

            # main paragraphs of opinion
            if opinion.get('paragraphs'):
                last_page_label = self.make_pars(opinion['paragraphs'], opinion_el, last_page_label=last_page_label, include_block_label=opinion['type']=='unprocessed')

            # <aside class='footnote'>, or <footnote>
            for footnote in opinion.get('footnotes', []):
                footnote_el = self.make_footnote_el(footnote)
                if footnote_el is None:
                    continue
                left_strip_text = None if self.original_xml else footnote.get('label', None)  # used for stripping footnote labels from text
                self.make_pars(footnote['paragraphs'], footnote_el, left_strip_text=left_strip_text)
                opinion_el.append(footnote_el)

            # special handling -- for xml, head matter goes directly under <case>
            if self.format == 'xml' and opinion['type'] in ('head', 'unprocessed'):
                for el in opinion_el:
                    case_el.append(el)
            else:
                case_el.append(opinion_el)

        # <section class='corrections'>, or <corrections>
        if self.corrections:
            if self.format == 'xml':
                for correction in self.corrections:
                    etree.SubElement(case_el, 'corrections').text = correction
            else:
                corrections_el = etree.SubElement(case_el, 'section', {'class': 'corrections'})
                for correction in self.corrections:
                    etree.SubElement(corrections_el, 'p').text = correction

        return etree.tostring(case_el, encoding=str)

    def make_case_el(self, case):
        """ Make <section class='case'>, or <casebody> """
        if self.format == 'xml':
            return etree.Element('casebody', {
                'firstpage': case.first_page or '',
                'lastpage': case.last_page or '',
                'xmlns': 'http://nrs.harvard.edu/urn-3:HLS.Libr.US_Case_Law.Schema.Case_Body_Duplicative:v1' if self.duplicative else 'http://nrs.harvard.edu/urn-3:HLS.Libr.US_Case_Law.Schema.Case_Body:v1',
            })
        else:
            return etree.Element('section', {'class': 'case', 'data-case-id': case.case_id})

    def make_opinion_el(self, opinion):
        """ Make <section class='opinion'>, or <opinion> """
        if self.format == 'xml':
            return etree.Element('opinion', {'type': opinion['type']})
        else:
            return etree.Element('section', {
                'class': 'head-matter' if opinion['type'] == 'head' else 'opinion ' + opinion['type'],
            })

    def make_footnote_el(self, footnote):
        """ Make <aside class='footnote'>, or <footnote> """
        if self.format == 'xml':
            footnote_attrs = {k: footnote[k] for k in ('label', 'orphan') if k in footnote}
            if footnote.get('redacted'):
                if self.redacted:
                    return None
                footnote_attrs['redact'] = 'true'
            return etree.Element('footnote', footnote_attrs)
        else:
            if self.redacted and footnote.get('redacted'):
                return None
            footnote_attrs = {'data-' + k: footnote[k] for k in ('label', 'orphan') if k in footnote}
            footnote_attrs['class'] = 'footnote'
            footnote_attrs['id'] = footnote.get('id', '')
            footnote_el = etree.Element('aside', footnote_attrs)
            if 'label' in footnote:
                etree.SubElement(footnote_el, 'a', {'href': '#ref_'+footnote['id']}).text = footnote['label']
            return footnote_el

    def make_pars(self, pars, parent_el, left_strip_text=None, last_page_label=None, include_block_label=False):
        """ Make each <p class='label'> or <label> element. """
        for par in pars:
            if self.redacted and par.get('redacted'):
                continue
            handler = sax.ElementTreeContentHandler()
            tag_stack = []

            # opening tag
            if self.format == 'xml':
                par_attrs = {'id': par['id']}

                # special handling for duplicative files -- alto block label gets applied as casemets paragraph label attr
                if include_block_label and par['block_ids']:
                    first_block = self.blocks_by_id[par['block_ids'][0]]
                    if 'class' in first_block and first_block['class'] != 'p':
                        par_attrs['label'] = first_block['class']

                tag_stack.append((handler.startElement, (par['class'], par_attrs,)))
            else:
                tag_stack.append((handler.startElement, ('p', {'class': par['class'], 'id': par['id']},)))

            # write each block in the paragraph
            for block_id in par['block_ids']:
                block = self.blocks_by_id[block_id]
                if self.redacted and block.get('redacted'):
                    continue

                # write <page-number> or <a class='page-label'> between blocks
                if not self.original_xml:
                    page_label = self.labels_by_block_id[block_id]
                    if page_label != last_page_label:
                        if last_page_label is not None:
                            if self.format == 'xml':
                                tag_stack.append((handler.startElement, ('page-number', {'label': page_label, 'citation-index': '1'},)))
                                tag_stack.append((handler.characters, ('*'+page_label,)))
                                tag_stack.append((handler.endElement, ('page-number',)))
                            else:
                                tag_stack.append((handler.startElement, ('a', {'id':'p'+page_label, 'href':'#p'+page_label, 'data-label':page_label, 'data-citation-index':'1', 'class':'page-label'},)))
                                tag_stack.append((handler.characters, ('*'+page_label,)))
                                tag_stack.append((handler.endElement, ('a',)))
                        last_page_label = page_label

                # write <img>
                if block.get('format') == 'image':
                    if self.format == 'xml':
                        tag_stack.append((handler.characters, ('[[Image here]]',)))
                    else:
                        tag_stack.append((handler.startElement, ('img', {'src': 'data:'+block['data'], 'class': block['class'], 'width': str(round(block['rect'][2])), 'height': str(round(block['rect'][3]))},)))
                        tag_stack.append((handler.endElement, ('img',)))

                # write tokens
                else:
                    open_font_tags = []
                    for token in filter_tokens(block.get('tokens'), self.html_token_filter, self.redacted):

                        # text token
                        if type(token) == str:
                            if left_strip_text:
                                while left_strip_text and token:
                                    if left_strip_text[0] == token[0]:
                                        left_strip_text = left_strip_text[1:]
                                        token = token[1:]
                                    else:
                                        left_strip_text = None
                            tag_stack.append((handler.characters, (token,)))
                            continue

                        token_name, token_attrs = (token + [{}])[:2]

                        # handle opening and closing font tags
                        if token_name == 'font':
                            if self.original_xml:
                                continue
                            font_obj = self.fonts_by_id[token_attrs['id']]
                            open_font_tags = [tag for tag, font_string in self.font_style_map if font_string in font_obj.style]
                            self.open_font_tags(handler, tag_stack, open_font_tags)
                        elif token_name == '/font':
                            if self.original_xml:
                                continue
                            self.close_font_tags(handler, tag_stack, open_font_tags)
                            open_font_tags = []

                        # handle footnotemark and bracketnum
                        elif token_name == 'footnotemark' or token_name == 'bracketnum':
                            if self.original_xml:
                                tag_stack.append((handler.startElement, (token_name,)))
                            elif self.format == 'xml':
                                with self.wrap_font_tags(handler, tag_stack, open_font_tags):
                                    tag_stack.append((handler.startElement, (token_name,)))
                            else:
                                attrs = {'class': token_name}
                                ref = token_attrs.get('ref')
                                if ref:
                                    attrs['href'] = '#' + ref
                                    attrs['id'] = 'ref_' + ref
                                with self.wrap_font_tags(handler, tag_stack, open_font_tags):
                                    tag_stack.append((handler.startElement, ('a', attrs,)))
                        elif token_name == '/footnotemark' or token_name == '/bracketnum':
                            with self.wrap_font_tags(handler, tag_stack, open_font_tags):
                                tag_stack.append((handler.endElement, (token_name[1:] if self.format == 'xml' else 'a',)))

            for method, args in tag_stack:
                method(*args)
            parent_el.append(handler._root)

        return last_page_label

    @staticmethod
    def open_font_tags(handler, tag_stack, font_tags):
        for tag in font_tags:
            last_el = tag_stack[-1]
            if last_el[0] == handler.endElement and last_el[1][0] == tag:
                tag_stack.pop()
            else:
                tag_stack.append((handler.startElement, (tag,)))

    @staticmethod
    def close_font_tags(handler, tag_stack, open_font_tags):
        for tag in reversed(open_font_tags):
            last_el = tag_stack[-1]
            if last_el[0] == handler.startElement and last_el[1][0] == tag:
                tag_stack.pop()
            else:
                tag_stack.append((handler.endElement, (tag,)))

    @contextmanager
    def wrap_font_tags(self, handler, tag_stack, open_font_tags):
        """ When opening or closing another tag, close and re-open all style tags (e.g. <em>, <strong>) """
        if open_font_tags:
            self.close_font_tags(handler, tag_stack, open_font_tags)
            yield
            self.open_font_tags(handler, tag_stack, open_font_tags)
        else:
            yield
