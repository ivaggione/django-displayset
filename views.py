import operator

from django.core.exceptions import PermissionDenied
from django.contrib.admin.views.main import ChangeList
from django.contrib.admin import options as adminoptions
from django.http import HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.db.models import Q
from django.db import models

class DefaultDisplaySite(object):
	actions = []
	root_path = '/'
	name = 'Default DisplaySet Site' 
	
	def admin_view(self,view):
		def no_wrap(request,*args,**kwargs):
			return view(request,*args,**kwargs)
		from django.views.decorators.csrf import csrf_protect
		from django.utils.functional import update_wrapper
		no_wrap = csrf_protect(no_wrap)
		return update_wrapper(no_wrap, view)

def generic(request,queryset,display_class,extra_context=None,display_site=DefaultDisplaySite):
	return display_class(queryset,display_site).changelist_view(request,extra_context)

ORDER_VAR = 'o'
ORDER_TYPE_VAR = 'ot'
class DisplayList(ChangeList):
	def __init__(self,queryset,request,*args,**kwargs):
		self.filtered_queryset = queryset
		super(DisplayList,self).__init__(request,*args,**kwargs)

	def get_ordering(self):
		lookup_opts, params = self.lookup_opts, self.params
		# For ordering, first check the "ordering" parameter in the admin
		# options, then check the object's default ordering. If neither of
		# those exist, order descending by ID by default. Finally, look for
		# manually-specified ordering from the query string.
		ordering = self.model_admin.ordering or lookup_opts.ordering or ['-' + lookup_opts.pk.name]

		if ordering[0].startswith('-'):
			order_field, order_type = ordering[0][1:], 'desc'
		else:
			order_field, order_type = ordering[0], 'asc'
		if ORDER_VAR in params:
			try:
				field_name = self.list_display[int(params[ORDER_VAR])]
				try:
					f = lookup_opts.get_field(field_name)
				except models.FieldDoesNotExist:
					# See whether field_name is a name of a non-field
					# that allows sorting.
					try:
						if callable(field_name):
							attr = field_name
						elif hasattr(self.model_admin, field_name):
							attr = getattr(self.model_admin, field_name)
						else:
							attr = getattr(self.model, field_name)
						order_field = attr.admin_order_field
					except AttributeError:
						if field_name in self.filtered_queryset.query.aggregates or field_name in self.filtered_queryset.query.extra: #****
							order_field = field_name
				else:
					order_field = f.name
			except (IndexError, ValueError):
				pass # Invalid ordering specified. Just use the default.
		if ORDER_TYPE_VAR in params and params[ORDER_TYPE_VAR] in ('asc', 'desc'):
			order_type = params[ORDER_TYPE_VAR]
		return order_field, order_type

	def get_query_set(self):
		# Set ordering.
		if self.order_field:
			self.filtered_queryset = self.filtered_queryset.order_by('%s%s' % ((self.order_type == 'desc' and '-' or ''), self.order_field))

		# Apply keyword searches.
		def construct_search(field_name):
			if field_name.startswith('^'):
				return "%s__istartswith" % field_name[1:]
			elif field_name.startswith('='):
				return "%s__iexact" % field_name[1:]
			elif field_name.startswith('@'):
				return "%s__search" % field_name[1:]
			else:
				return "%s__icontains" % field_name

		if self.search_fields and self.query:
			for bit in self.query.split():
				or_queries = [Q(**{construct_search(str(field_name)): bit}) for field_name in self.search_fields]
				self.filtered_queryset = self.filtered_queryset.filter(reduce(operator.or_, or_queries))
			for field_name in self.search_fields:
				if '__' in field_name:
					self.filtered_queryset = self.filtered_queryset.distinct()
					break

		return self.filtered_queryset
		
class DisplaySet(adminoptions.ModelAdmin):
	change_list_template = 'displayset/base.html'

	def __init__(self,queryset,display_set_site,*args,**kwargs):
		self.filtered_queryset = queryset
		super(DisplaySet,self).__init__(queryset.model,display_set_site)
	
	def queryset(self, request):
		return self.filtered_queryset
	
	def changelist_view(self, request, extra_context=None):
		"The 'change list' admin view for this model."
		from django.contrib.admin.views.main import ERROR_FLAG
		from django.contrib.admin.options import IncorrectLookupParameters
		opts = self.model._meta
		app_label = opts.app_label
		
		# Check actions to see if any are available on this changelist
		actions = self.get_actions(request)

		# Remove action checkboxes if there aren't any actions available.
		list_display = list(self.list_display)
		if not actions:
			try:
				list_display.remove('action_checkbox')
			except ValueError:
				pass

		try:
			cl = DisplayList(self.filtered_queryset,request, self.model, list_display, self.list_display_links, self.list_filter,
		self.date_hierarchy, self.search_fields, self.list_select_related, self.list_per_page, self.list_editable, self)
		except IncorrectLookupParameters:
			# Wacky lookup parameters were given, so redirect to the main
			# changelist page, without parameters, and pass an 'invalid=1'
			# parameter via the query string. If wacky parameters were given and
			# the 'invalid=1' parameter was already in the query string, something
			# is screwed up with the database, so display an error page.
			if ERROR_FLAG in request.GET.keys():
				return render_to_response('admin/invalid_setup.html', {'title': 'Database error'})
			return HttpResponseRedirect(request.path + '?' + ERROR_FLAG + '=1')

		# If the request was POSTed, this might be a bulk action or a bulk edit.
		# Try to look up an action first, but if this isn't an action the POST
		# will fall through to the bulk edit check, below.
		if actions and request.method == 'POST':
			response = self.response_action(request, queryset=cl.get_query_set())
			if response:
				return response

		# If we're allowing changelist editing, we need to construct a formset
		# for the changelist given all the fields to be edited. Then we'll
		# use the formset to validate/process POSTed data.
		formset = cl.formset = None

		# Handle POSTed bulk-edit data.
		if request.method == "POST" and self.list_editable:
			FormSet = self.get_changelist_formset(request)
			formset = cl.formset = FormSet(request.POST, request.FILES, queryset=cl.result_list)
			if formset.is_valid():
				changecount = 0
				for form in formset.forms:
					if form.has_changed():
						obj = self.save_form(request, form, change=True)
						self.save_model(request, obj, form, change=True)
						form.save_m2m()
						change_msg = self.construct_change_message(request, form, None)
						self.log_change(request, obj, change_msg)
						changecount += 1

				if changecount:
					if changecount == 1:
						name = force_unicode(opts.verbose_name)
					else:
						name = force_unicode(opts.verbose_name_plural)
					msg = ungettext("%(count)s %(name)s was changed successfully.",
									"%(count)s %(name)s were changed successfully.",
									changecount) % {'count': changecount,
													'name': name,
													'obj': force_unicode(obj)}
					self.message_user(request, msg)

				return HttpResponseRedirect(request.get_full_path())

		# Handle GET -- construct a formset for display.
		elif self.list_editable:
			FormSet = self.get_changelist_formset(request)
			formset = cl.formset = FormSet(queryset=cl.result_list)

		# Build the list of media to be used by the formset.
		if formset:
			media = self.media + formset.media
		else:
			media = self.media

		# Build the action form and populate it with available actions.
		if actions:
			action_form = self.action_form(auto_id=None)
			action_form.fields['action'].choices = self.get_action_choices(request)
		else:
			action_form = None

		context = {
			'title': cl.title,
			'is_popup': cl.is_popup,
			'cl': cl,
			'media': media,
			'has_add_permission': self.has_add_permission(request),
			'root_path': self.admin_site.root_path,
			'app_label': app_label,
			'action_form': action_form,
			'actions_on_top': self.actions_on_top,
			'actions_on_bottom': self.actions_on_bottom,
		}
		context.update(extra_context or {})
		context_instance = RequestContext(request, current_app=self.admin_site.name)
		return render_to_response(self.change_list_template or [
			'admin/%s/%s/change_list.html' % (app_label, opts.object_name.lower()),
			'admin/%s/change_list.html' % app_label,
			'admin/change_list.html'
		], context, context_instance=context_instance)
	

